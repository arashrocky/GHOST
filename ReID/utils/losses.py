import torch
from torch import nn
import torch.nn.functional as F
from torch.autograd import Variable

def reduce_loss(loss, reduction):
    """Reduce loss as specified.
    
    https://mmdetection.readthedocs.io/en/v2.2.0/_modules/mmdet/models/losses/utils.html#weight_reduce_loss

    Args:
        loss (Tensor): Elementwise loss tensor.
        reduction (str): Options are "none", "mean" and "sum".

    Return:
        Tensor: Reduced loss tensor.
    """
    reduction_enum = F._Reduction.get_enum(reduction)
    # none: 0, elementwise_mean:1, sum: 2
    if reduction_enum == 0:
        return loss
    elif reduction_enum == 1:
        return loss.mean()
    elif reduction_enum == 2:
        return loss.sum()


def weight_reduce_loss(loss, weight=None, reduction='mean', avg_factor=None):
    """
    
    https://mmdetection.readthedocs.io/en/v2.2.0/_modules/mmdet/models/losses/utils.html#weight_reduce_loss

    Apply element-wise weight and reduce loss.

    Args:
        loss (Tensor): Element-wise loss.
        weight (Tensor): Element-wise weights.
        reduction (str): Same as built-in losses of PyTorch.
        avg_factor (float): Avarage factor when computing the mean of losses.

    Returns:
        Tensor: Processed loss values.
    """

    # if weight is specified, apply element-wise weight
    if weight is not None:
        loss = loss * weight

    # if avg_factor is not specified, just reduce the loss
    if avg_factor is None:
        loss = reduce_loss(loss, reduction)
    else:
        # if reduction is mean, then average the loss by avg_factor
        if reduction == 'mean':
            loss = loss.sum() / avg_factor
        # if reduction is 'none', then do nothing, otherwise raise an error
        elif reduction != 'none':
            raise ValueError('avg_factor can not be used with reduction="sum"')
    return loss

# MultiPositiveContrastive
class MultiPositiveContrastive(torch.nn.Module):
    """Cross entropy loss with label smoothing regularizer.
    Reference:
    Szegedy et al. Rethinking the Inception Architecture for Computer Vision. CVPR 2016.
    Equation: y = (1 - epsilon) * y + epsilon / K.
    Args:
        num_classes (int): number of classes.
        epsilon (float): weight.
    """
    def __init__(self):
        super(MultiPositiveContrastive, self).__init__()

    def forward(self, inputs, targets, weight=None,
                    reduction='mean', avg_factor=None):
        """
        Args:
            inputs: prediction matrix (before softmax) with shape (batch_size, num_classes)
            targets: ground truth labels with shape (batch_size, num_classes)
        """
        
        targets = torch.atleast_2d(targets)
        mask = targets == targets.T

        #test without for loop 
        inp = inputs[:, targets[0]].T # without transpose each row contains one samples prob but we need other way round 

        pos_inds = mask
        neg_inds = ~mask
        pos = pos_inds.float()
        neg = neg_inds.float()

        _pos = inp * pos
        _neg = inp * neg

        _pos[neg_inds] = _pos[neg_inds] + float('inf')
        _neg[pos_inds] = _neg[pos_inds] + float('-inf')

        _pos_expand = torch.repeat_interleave(_pos, inputs.shape[0], dim=1)
        _neg_expand = _neg.repeat(1, inputs.shape[0])

        x = torch.nn.functional.pad((_neg_expand - _pos_expand), (0, 1), "constant", 0)
        loss = torch.logsumexp(x, dim=1)
        
        if weight is not None:
            weight = weight.float()

        loss = weight_reduce_loss(loss, weight=weight, reduction=reduction, avg_factor=avg_factor)

        return loss


# cross entropy and center loss
class CrossEntropyDistill(torch.nn.Module):
    """Cross entropy loss with label smoothing regularizer.
    Reference:
    Szegedy et al. Rethinking the Inception Architecture for Computer Vision. CVPR 2016.
    Equation: y = (1 - epsilon) * y + epsilon / K.
    Args:
        num_classes (int): number of classes.
        epsilon (float): weight.
    """
    def __init__(self):
        super(CrossEntropyDistill, self).__init__()
        self.logsoftmax = torch.nn.LogSoftmax(dim=1)

    def forward(self, inputs, targets):
        """
        Args:
            inputs: prediction matrix (before softmax) with shape (batch_size, num_classes)
            targets: ground truth labels with shape (batch_size, num_classes)
        """
        log_probs = self.logsoftmax(inputs)
        loss = (- targets * log_probs).mean(0).sum()
        return loss


class KLDivWithLogSM(torch.nn.Module):
    def __init__(self):
        super(KLDivWithLogSM, self).__init__()
        self.logsoftmax = torch.nn.LogSoftmax(dim=1)
        self.KLDiv = torch.nn.KLDivLoss()

    def forward(self, inputs, targets):
        """
        Args:
            inputs: prediction matrix (before softmax) with shape (batch_size, num_classes)
            targets: ground truth labels with shape (batch_size, num_classes)
        """
        log_probs = self.logsoftmax(inputs)
        loss = self.KLDiv(log_probs, targets) 
        return loss


class DistanceLoss(torch.nn.Module):
    def __init__(self):
        super(DistanceLoss, self).__init__()
        #self.l2_teacher = torch.nn.MSELoss(reduce=False)
        #self.l2_student = torch.nn.MSELoss(reduce=False)
        #print('mean') 
        self.l2_teacher = torch.nn.SmoothL1Loss(reduce=False)
        self.l2_student = torch.nn.SmoothL1Loss(reduce=False)

    def forward(self, teacher, student):
        num_samps = teacher.shape[0]
        i1 = torch.tensor([i for i in range(num_samps) for j in range(num_samps)]).cuda(student.get_device())
        i2 = torch.tensor([j for i in range(num_samps) for j in range(num_samps)]).cuda(student.get_device())

        teacher_l2s = torch.sum(self.l2_teacher(teacher.index_select(0, i1), teacher.index_select(0, i2)), dim=1)
        teacher_l2s = teacher_l2s/teacher_l2s.mean()
        student_l2s = torch.sum(self.l2_student(student.index_select(0, i1), student.index_select(0, i2)), dim=1)
        student_l2s = student_l2s/student_l2s.mean()
        return torch.sum(torch.abs(teacher_l2s - student_l2s))
        


# cross entropy and center loss
class CrossEntropyLabelSmooth(torch.nn.Module):
    """Cross entropy loss with label smoothing regularizer.
    Reference:
    Szegedy et al. Rethinking the Inception Architecture for Computer Vision. CVPR 2016.
    Equation: y = (1 - epsilon) * y + epsilon / K.
    Args:
        num_classes (int): number of classes.
        epsilon (float): weight.
    """
    def __init__(self, num_classes, dev=0, epsilon=0.1, use_gpu=True):
        super(CrossEntropyLabelSmooth, self).__init__()
        self.num_classes = num_classes
        self.epsilon = epsilon
        self.use_gpu = use_gpu
        self.dev = dev
        self.logsoftmax = torch.nn.LogSoftmax(dim=1)

    def forward(self, inputs, targets):
        """
        Args:
            inputs: prediction matrix (before softmax) with shape (batch_size, num_classes)
            targets: ground truth labels with shape (num_classes)
        """
        log_probs = self.logsoftmax(inputs)
        targets = torch.zeros(log_probs.size()).scatter_(1, targets.unsqueeze(1).data.cpu(), 1)
        if self.use_gpu: targets = targets.cuda(self.dev)
        targets = (1 - self.epsilon) * targets + self.epsilon / self.num_classes
        loss = (- targets * log_probs).mean(0).sum()
        return loss


class FocalLoss(nn.Module):
    def __init__(self, gamma=7, alpha=None, size_average=True):
        super(FocalLoss, self).__init__()
        print("Focal Loss: Gamma = {}".format(gamma))
        self.gamma = gamma
        self.alpha = alpha
        if isinstance(alpha,(float,int)): self.alpha = torch.Tensor([alpha,1-alpha])
        if isinstance(alpha,list): self.alpha = torch.Tensor(alpha)
        self.size_average = size_average

    def forward(self, input, target):
        if input.dim()>2:
            input = input.view(input.size(0),input.size(1),-1)  # N,C,H,W => N,C,H*W
            input = input.transpose(1,2)    # N,C,H*W => N,H*W,C
            input = input.contiguous().view(-1,input.size(2))   # N,H*W,C => N*H*W,C
        target = target.view(-1,1)

        logpt = F.log_softmax(input)
        logpt = logpt.gather(1,target)
        logpt = logpt.view(-1)
        pt = Variable(logpt.data.exp())

        if self.alpha is not None:
            if self.alpha.type()!=input.data.type():
                self.alpha = self.alpha.type_as(input.data)
            at = self.alpha.gather(0,target.data.view(-1))
            logpt = logpt * Variable(at)

        loss = -1 * (1-pt)**self.gamma * logpt
        if self.size_average: return loss.mean()
        else: return loss.sum()


class CenterLoss(torch.nn.Module):
    """Center loss.
    Reference:
    Wen et al. A Discriminative Feature Learning Approach for Deep Face Recognition. ECCV 2016.
    Args:
        num_classes (int): number of classes.
        feat_dim (int): feature dimension.
    """

    def __init__(self, num_classes=751, feat_dim=512, use_gpu=True):
        super(CenterLoss, self).__init__()
        self.num_classes = num_classes
        self.feat_dim = feat_dim
        self.use_gpu = use_gpu

        if self.use_gpu:
            self.centers = torch.nn.Parameter(torch.randn(self.num_classes, self.feat_dim).cuda())
        else:
            self.centers = torch.nn.Parameter(torch.randn(self.num_classes, self.feat_dim))

    def forward(self, x, labels):
        """
        Args:
            x: feature matrix with shape (batch_size, feat_dim).
            labels: ground truth labels with shape (num_classes).
        """
        assert x.size(0) == labels.size(0), "features.size(0) is not equal to labels.size(0)"

        batch_size = x.size(0)
        distmat = torch.pow(x, 2).sum(dim=1, keepdim=True).expand(batch_size, self.num_classes) + \
                  torch.pow(self.centers, 2).sum(dim=1, keepdim=True).expand(self.num_classes, batch_size).t()
        distmat.addmm_(1, -2, x, self.centers.t())

        classes = torch.arange(self.num_classes).long()
        if self.use_gpu: classes = classes.cuda()
        labels = labels.unsqueeze(1).expand(batch_size, self.num_classes)
        mask = labels.eq(classes.expand(batch_size, self.num_classes))

        dist = distmat * mask.float()
        loss = dist.clamp(min=1e-12, max=1e+12).sum() / batch_size
        return loss


class TripletLoss(nn.Module):
    def __init__(self, margin=0.1):
        super(TripletLoss, self).__init__()
        self.margin = margin
        self.ranking_loss = nn.MarginRankingLoss(margin=margin)

    def forward(self, inputs=None, targets=None, dist=None):
        # Compute pairwise distance
        if dist is None:
            m, n = inputs.size(0), inputs.size(0)
            x = inputs.view(m, -1)
            y = inputs.view(n, -1)
            dist = torch.pow(x, 2).sum(dim=1, keepdim=True).expand(m, n) + \
                torch.pow(y, 2).sum(dim=1, keepdim=True).expand(n, m).t()

            dist.addmm_(1, -2, x, y.t())

        mask = targets.type(torch.ByteTensor).cuda()

        # for numerical stability
        # For each anchor, find the hardest positive and negative
        #mask = targets.expand(n, n).eq(targets.expand(n, n).t())
        dist_ap, dist_an = [], []
        for i in range(n):
            mask = targets == targets[i]
            dist_ap.append(dist[i][mask].max().unsqueeze(dim=0)) #hp
            dist_an.append(dist[i][mask == 0].min().unsqueeze(dim=0)) #hn
        dist_ap = torch.cat(dist_ap)
        dist_an = torch.cat(dist_an)
        # Compute ranking hinge loss
        y = dist_an.data.new()

        y.resize_as_(dist_an.data)
        y.fill_(1)
        y = Variable(y)
        loss = self.ranking_loss(dist_an, dist_ap, y)
        prec = (dist_an.data > dist_ap.data).sum() * 1. / y.size(0)
        return loss, prec


class TripletLoss(nn.Module):
    def __init__(self, margin=0.1):
        super(TripletLoss, self).__init__()
        self.margin = margin
        self.ranking_loss = nn.MarginRankingLoss(margin=margin)

    def forward(self, inputs, targets):
        # Compute pairwise distance
        m, n = inputs.size(0), inputs.size(0)
        x = inputs.view(m, -1)
        y = inputs.view(n, -1)
        dist = torch.pow(x, 2).sum(dim=1, keepdim=True).expand(m, n) + \
               torch.pow(y, 2).sum(dim=1, keepdim=True).expand(n, m).t()

        dist.addmm_(1, -2, x, y.t())

        mask = targets.type(torch.ByteTensor).cuda()

        # for numerical stability
        # For each anchor, find the hardest positive and negative
        #mask = targets.expand(n, n).eq(targets.expand(n, n).t())
        dist_ap, dist_an = [], []
        for i in range(n):
            mask = targets == targets[i]
            dist_ap.append(dist[i][mask].max().unsqueeze(dim=0)) #hp
            dist_an.append(dist[i][mask == 0].min().unsqueeze(dim=0)) #hn
        dist_ap = torch.cat(dist_ap)
        dist_an = torch.cat(dist_an)
        # Compute ranking hinge loss
        y = dist_an.data.new()

        y.resize_as_(dist_an.data)
        y.fill_(1)
        y = Variable(y)
        loss = self.ranking_loss(dist_an, dist_ap, y)
        prec = (dist_an.data > dist_ap.data).sum() * 1. / y.size(0)
        return loss, prec


# Multi positive contrastive from QD-Track

def multi_pos_cross_entropy(pred,
                            label,
                            weight=None,
                            reduction='mean',
                            avg_factor=None):
    #Multi positive contrastive from QD-Track

    # element-wise losses
    # pos_inds = (label == 1).float()
    # neg_inds = (label == 0).float()
    # exp_pos = (torch.exp(-1 * pred) * pos_inds).sum(dim=1)
    # exp_neg = (torch.exp(pred.clamp(max=80)) * neg_inds).sum(dim=1)
    # loss = torch.log(1 + exp_pos * exp_neg)    

    pos_inds = label
    neg_inds = ~label

    pred_pos = pred * pos_inds.float()
    pred_neg = pred * neg_inds.float()
    # use -inf to mask out unwanted elements.
    # --> don't take distance to itself into account (should be in pos anyway)

    self_dist = torch.diag(torch.ones(label.shape[0])).bool().to(neg_inds.get_device())
    pred_pos[neg_inds | self_dist] = pred_pos[neg_inds | self_dist] + float('inf')
    pred_neg[pos_inds] = pred_neg[pos_inds] + float('-inf')

    _pos_expand = torch.repeat_interleave(pred_pos, pred.shape[1], dim=1)
    _neg_expand = pred_neg.repeat(1, pred.shape[1])

    x = torch.nn.functional.pad((_neg_expand - _pos_expand), (0, 1), "constant", 0)
    loss = torch.logsumexp(x, dim=1)

    # apply weights and do the reduction
    if weight is not None:
        weight = weight.float()
    loss = weight_reduce_loss(
        loss, weight=weight, reduction=reduction, avg_factor=avg_factor)

    return loss


class MultiPosCrossEntropyLoss(nn.Module):
    #Multi positive contrastive from QD-Track

    def __init__(self, reduction='mean', loss_weight=1.0):
        super(MultiPosCrossEntropyLoss, self).__init__()
        self.reduction = reduction
        self.loss_weight = loss_weight

    def forward(self,
                cls_score,
                label,
                weight=None,
                avg_factor=None,
                reduction_override=None,
                **kwargs):
        label = torch.atleast_2d(label)
        label = label == label.T
        assert cls_score.size() == label.size()
        assert reduction_override in (None, 'none', 'mean', 'sum')
        reduction = (
            reduction_override if reduction_override else self.reduction)
        loss_cls = self.loss_weight * multi_pos_cross_entropy(
            cls_score,
            label,
            weight,
            reduction=reduction,
            avg_factor=avg_factor,
            **kwargs)
        return loss_cls
