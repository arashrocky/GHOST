from collections import defaultdict
import copy
from re import L
from matplotlib.pyplot import imsave
from numpy.core.fromnumeric import size

from pandas.core.indexing import IndexingMixin
import torch
import sklearn.metrics
#from tracking_wo_bnw.src.tracktor.utils import interpolate
import os
import numpy as np
import logging
from lapsolver import solve_dense
import json
from math import floor
from src.tracking_utils import bisoftmax, get_proxy, add_ioa, Track  # , get_reid_performance
from src.base_tracker import BaseTracker
import torchvision.transforms as T
import torch.nn.functional as F


logger = logging.getLogger('AllReIDTracker.Tracker')


class Tracker(BaseTracker):
    def __init__(
            self,
            tracker_cfg,
            encoder,
            net_type='resnet50',
            output='plain',
            weight='No',
            data='tracktor_preprocessed_files.txt'):
        super(
            Tracker,
            self).__init__(
            tracker_cfg,
            encoder,
            net_type,
            output,
            weight,
            data)

    def track(self, seq, first=False):
        '''
        first - feed all bounding boxes through net first for bn stats update
        seq -   sequence instance for iteratration and with meta information
                like name or lentgth
        '''
        logger.info(
            "Tracking sequence {} of lenght {}".format(
                seq.name, seq.num_frames))
        self.setup_seq(seq, first)

        # batch norm experiemnts I
        self.normalization_before(seq, first)

        # iterate over frames
        for i, (frame, _, path, boxes, _, gt_ids, vis,
                random_patches, whole_im, frame_size, areas_out) in enumerate(seq):

            # batch norm experiments II
            self.normalization_experiments(random_patches, frame, i)

            self.frame_id = int(path.split(os.sep)[-1][:-4])
            # print(i+1, self.frame_id, len(self.tracks), len(self.inactive_tracks))

            if self.debug:
                self.event_dict[self.seq][self.frame_id] = list()
            detections = list()

            # forward pass
            with torch.no_grad():
                if self.net_type == 'resnet50_analysis':
                    feats = self.encoder(frame)
                else:
                    _, feats = self.encoder(frame, output_option=self.output)

            # add features of whole blurred image
            if self.tracker_cfg['use_blur']:
                blurred_feats = self.get_blurred_feats(whole_im, boxes)
                feats = torch.cat([feats, blurred_feats], dim=1)
                # feats = feats + blurred_feats

            # just feeding for bn stats update
            if first:
                continue

            # iterate over bbs in current frame
            for f, b, gt_id, v, a in zip(feats, boxes, gt_ids, vis, areas_out):
                if (b[3] - b[1]) / (b[2] - b[0]
                                    ) < self.tracker_cfg['h_w_thresh']:
                    detection = {
                        'bbox': b,
                        'feats': f,
                        'im_index': self.frame_id,
                        'gt_id': gt_id,
                        'vis': v,
                        'area_out': a}
                    detections.append(detection)

                    if self.store_dist:
                        v = 0.999 if v == 1.0 else v
                        v = floor(v * 10) / 10
                        self.distance_[
                            self.seq]['visibility_count'][v] += 1

                    if self.save_embeddings_by_id:
                        _f = f.cpu().numpy().tolist()
                        self.embeddings_by_id[seq.name][gt_id].append([i, _f])

            # apply motion compensation to stored track positions
            if self.motion_model_cfg['motion_compensation']:
                self.motion_compensation(whole_im, i)

            # add intersection over area to each bb
            self.curr_interaction, self.curr_occlusion = add_ioa(
                detections,
                self.seq,
                self.interaction,
                self.occlusion,
                frame_size)

            # association over frames
            tr_ids = self._track(detections, i, frame=frame)

            if self.store_visualization:
                self.visualize(detections, tr_ids, path, seq.name, i+1)

        # just fed for bn stats update
        if first:
            logger.info('Done with pre-tracking feed...')
            return

        # add inactive tracks to active tracks for evaluation
        self.tracks.update(self.inactive_tracks)

        # compute reid performance
        if self.tracker_cfg['get_reid_performance']:
            cmc, aps = get_reid_performance()
            logger.info(
                "r-1 {}, r-5 {}, r-8 {}".format(cmc[0], cmc[5], cmc[7]))
            logger.info("mAP {}".format(np.mean(aps)))

        # write results
        self.write_results(self.output_dir, seq.name)

        # reset thresholds if every / tbd
        self.reset_threshs()

        # store dist to json file
        if self.store_dist:
            logger.info(
                "Storing distance information of {} to {}".format(
                    seq.name,
                    self.experiment +
                    'distances.json'))
            with open(self.experiment + 'distances.json', 'w') as jf:
                json.dump(self.distance_, jf)

        # print errors and store error events
        if self.debug:
            if '13' in seq.name:
                logger.info(self.errors)
            with open(self.experiment + 'event_dict.json', 'w') as jf:
                json.dump(self.event_dict, jf)

        # save embeddings by id for further investigation
        if self.save_embeddings_by_id:
            with open(self.experiment + 'embeddings_by_id.json', 'w') as jf:
                json.dump(self.embeddings_by_id, jf)

    def get_blurred_feats(self, whole_im, boxes):
        blurrer = T.GaussianBlur(kernel_size=(29, 29), sigma=5)
        self.encoder.eval()
        ims = list()
        for box in boxes:
            blurred = copy.deepcopy(whole_im)
            blurred = blurrer(blurred)
            blurred[:, int(box[1]):int(box[3]), int(box[0]):int(box[2])] = \
                whole_im[:, int(box[1]):int(box[3]), int(box[0]):int(box[2])]
            blurred = F.interpolate(blurred.unsqueeze(dim=0), scale_factor=(1/8, 1/8))
            ims.append(blurred)

        ims = torch.stack(ims).squeeze()
        if len(ims.shape) < 4:
            ims = ims.unsqueeze(0)
        with torch.no_grad():
            if self.net_type == 'resnet50_analysis':
                blurred_feats = self.encoder(ims)
            else:
                _, blurred_feats = self.encoder(ims, output_option=self.output)

        self.encoder.train()

        return blurred_feats

    def _track(self, detections, i, frame=None):
        # just add all bbs to self.tracks / intitialize in the first frame
        if i == 0:
            tr_ids = list()
            for detection in detections:
                self.tracks[self.id] = Track(track_id=self.id, **detection)
                tr_ids.append(self.id)
                self.id += 1

        # association over frames for frame > 0
        elif i > 0:
            # get hungarian matching
            if not self.tracker_cfg['avg_inact']['proxy'] == 'each_sample':
                dist, row, col, ids = self.get_hungarian_with_proxy(
                    detections, sep=self.tracker_cfg['assign_separately'])
            else:
                dist, row, col, ids = self.get_hungarian_each_sample(
                    detections, sep=self.tracker_cfg['assign_separately'])

            if dist is not None:
                # get bb assignment
                tr_ids = self.assign(
                    detections=detections,
                    dist=dist,
                    row=row,
                    col=col,
                    ids=ids,
                    sep=self.tracker_cfg['assign_separately'])
        return tr_ids

    def get_hungarian_each_sample(self, detections, sep=False):
        # get new detections
        x = torch.stack([t['feats'] for t in detections])
        gt_n = [v['gt_id'] for v in detections]
        height = [int(v['bbox'][3]-v['bbox'][1]) for v in detections]

        # get distances to active tracks
        gt_t, dist_all, ids = list(), list(), list()
        gt_t += [track.gt_id for track in self.tracks.values()]

        # if use each sample for active frames
        if not self.tracker_cfg['avg_act']['do'] and len(detections) > 0:
            y = torch.stack([t.feats for t in self.tracks.values()])
            ids.extend([i for i in self.tracks.keys()])
            dist = sklearn.metrics.pairwise_distances(
                x.cpu().numpy(), y.cpu().numpy(), metric=self.tracker_cfg['distance']).T
            dist_all.extend([d for d in dist])
        else:
            for id, tr in self.tracks.items():
                y = torch.stack(tr.past_feats)
                ids.append(id)
                dist = sklearn.metrics.pairwise_distances(
                    x.cpu().numpy(), y.cpu().numpy(), metric=self.tracker_cfg['distance'])

                if self.tracker_cfg['avg_act']['num'] == 1:
                    dist_all.append(np.min(dist, axis=1))
                elif self.tracker_cfg['avg_act']['num'] == 2:
                    dist_all.append(np.mean(dist, axis=1))
                elif self.tracker_cfg['avg_act']['num'] == 3:
                    dist_all.append(np.max(dist, axis=1))
                elif self.tracker_cfg['avg_act']['num'] == 4:
                    dist_all.append(
                        (np.max(dist, axis=1) + np.min(dist, axis=1)) / 2)

        num_active = len(ids)
        
        # get distances to inactive tracklets (inacht thresh = 100000)
        curr_it = {k: track for k, track in self.inactive_tracks.items()
                   if track.inactive_count <= self.inact_thresh}
        if len(curr_it) > 0:
            gt_t += [track.gt_id for track in curr_it.values()]
            for id, tr in curr_it.items():
                y = torch.stack(tr.past_feats)
                ids.append(id)
                dist = sklearn.metrics.pairwise_distances(
                    x.cpu().numpy(), y.cpu().numpy(), metric=self.tracker_cfg['distance'])
                if self.tracker_cfg['avg_inact']['num'] == 1:
                    dist_all.append(np.min(dist, axis=1))
                elif self.tracker_cfg['avg_inact']['num'] == 2:
                    dist_all.append(np.mean(dist, axis=1))
                elif self.tracker_cfg['avg_inact']['num'] == 3:
                    dist_all.append(np.max(dist, axis=1))
                elif self.tracker_cfg['avg_inact']['num'] == 4:
                    dist_all.append(
                        (np.max(dist, axis=1) + np.min(dist, axis=1)) / 2)
        num_inactive = len(curr_it)

        # solve assignment problem
        dist = np.vstack(dist_all).T

        # update thresholds
        self.update_thresholds(dist, num_active, num_inactive)

        if self.motion_model_cfg['apply_motion_model']:
            self.motion()
            iou = self.get_motion_dist(detections, curr_it)
            inactive_counts = [it.inactive_count for it in curr_it.values()]
            dist = self.combine_motion_appearance(iou, dist, detections, num_active, num_inactive, inactive_counts, gt_n, gt_t)

        if self.nan_first:
            dist[:, :num_active] = np.where(dist[:, :num_active] <=
                self.act_reid_thresh, dist[:, :num_active], np.nan)
            dist[:, num_active:] = np.where(dist[:, num_active:] <=
                self.inact_reid_thresh, dist[:, num_active:], np.nan)

        if self.tracker_cfg['active_proximity']:
            self.proximity = self.active_proximity(dist, num_active, detections)

        # store distances
        if self.store_dist:
            self.add_dist_to_storage(
                gt_n, gt_t, num_active, num_inactive, dist, height)

        if not sep:
            row, col = solve_dense(dist)
        else:
            dist_act = dist[:, :num_active]
            row, col = solve_dense(dist_act)
            if num_active > 0:
                dist_inact = dist[:, num_active:]
            else:
                dist_inact = None
            dist = [dist_act, dist_inact]

        return dist, row, col, ids

    def get_hungarian_with_proxy(self, detections, sep=False):
        # instantiate
        ids, gt_t = list(), list()
        y_inactive, y = None, None

        x = torch.stack([t['feats'] for t in detections])
        gt_n = [v['gt_id'] for v in detections]
        height = [int(v['bbox'][3]-v['bbox'][1]) for v in detections]

        # Get active tracklets
        if len(self.tracks) > 0:
            if self.tracker_cfg['avg_act']['do']:
                y = get_proxy(
                    curr_it=self.tracks,
                    mode='act',
                    tracker_cfg=self.tracker_cfg,
                    mv_avg=self.mv_avg)
            else:
                y = torch.stack([track.feats for track in self.tracks.values()])

            ids += list(self.tracks.keys())
            gt_t += [track.gt_id for track in self.tracks.values()]
            num_active = len(ids)

        # get inactive tracklets (inacht thresh = 100000)
        curr_it = {k: track for k, track in self.inactive_tracks.items()
                   if track.inactive_count <= self.inact_thresh}
        # if there are inactive tracks that fall into inact_thresh
        if len(curr_it) > 0:
            if self.tracker_cfg['avg_inact']['do']:
                y_inactive = get_proxy(
                    curr_it=curr_it,
                    mode='inact',
                    tracker_cfg=self.tracker_cfg,
                    mv_avg=self.mv_avg)
            else:
                y_inactive = torch.stack([track.feats
                                         for track in curr_it.values()])

            if len(self.tracks) > 0 and not sep:
                y = torch.cat([y, y_inactive])
            elif not sep:
                y = y_inactive
                num_active = 0

            ids += [k for k in curr_it.keys()]
            gt_t += [track.gt_id for track in curr_it.values()]
            num_inactive = len(curr_it)

        # if no active or inactive tracks --> return and instantiate all dets
        # new
        elif len(curr_it) == 0 and len(self.tracks) == 0:
            for detection in detections:
                self.tracks[self.id] = Track(track_id=self.id, **detection)
                self.id += 1
            return None, None, None, None
        # if there are active but no inactive
        else:
            num_inactive = 0

        # compute distance
        if not sep:
            if not self.tracker_cfg['use_bism']:
                dist = sklearn.metrics.pairwise_distances(
                    x.cpu().numpy(), y.cpu().numpy(), metric=self.tracker_cfg['distance'])
            else:
                dist = 1 - bisoftmax(x.cpu(), y.cpu())

            if self.store_dist:
                self.add_dist_to_storage(
                    gt_n, gt_t, num_active, num_inactive, dist, height)

            # update thresholds
            self.update_thresholds(dist, num_active, num_inactive)

            if self.nan_first:
                dist[:, :num_active] = np.where(dist[:, :num_active] <=
                    self.act_reid_thresh, dist[:, :num_active], np.nan)
                dist[:, num_active:] = np.where(dist[:, num_active:] <=
                    self.act_reid_thresh, dist[:, num_active:], np.nan)

            # row represent current frame
            # col represents last frame + inactiva tracks
            # row, col = scipy.optimize.linear_sum_assignment(dist)
            row, col = solve_dense(dist)
        else:
            dist_act = sklearn.metrics.pairwise_distances(
                x.cpu().numpy(), y.cpu().numpy(), metric=self.tracker_cfg['distance'])
            row, col = solve_dense(dist_act)
            if y_inactive is not None:
                dist_inact = sklearn.metrics.pairwise_distances(
                    x.cpu().numpy(),
                    y_inactive.cpu().numpy(),
                    metric=self.tracker_cfg['distance'])  # 'euclidean')#'cosine')
            else:
                dist_inact = None
            dist = [dist_act, dist_inact]

        return dist, row, col, ids

    def assign(self, detections, dist, row, col, ids, sep=False):
        # assign tracks from hungarian
        active_tracks = list()
        tr_ids = [None for _ in range(len(detections))]

        if not sep:
            assigned = self.assign_act_inact_same_time(
                row, col, dist, detections, active_tracks, ids, tr_ids)
        else:
            assigned = self.assign_separatly(
                row, col, dist, detections, active_tracks, ids, tr_ids)

        # move tracks not used to inactive tracks
        keys = list(self.tracks.keys())
        for k in keys:
            if k not in active_tracks:
                self.inactive_tracks[k] = self.tracks[k]
                del self.tracks[k]
                self.inactive_tracks[k].inactive_count += 0

        # increase inactive count by one
        for k in self.inactive_tracks.keys():
            self.inactive_tracks[k].inactive_count += 1

        # tracks that have not been assigned by hungarian
        if self.debug:
            gt_available = [track.gt_id for track in self.tracks.values()]
            gt_available_k = [k for k in self.tracks.keys()]
            gt_available_b = [track.gt_id for track in self.inactive_tracks.values()]
            gt_available_bk = [k for k in self.inactive_tracks.keys()]

        for i in range(len(detections)):
            if i not in assigned:
                if self.debug:
                    ioa = self.round1(detections[i]['ioa'])
                    vis = self.round1(detections[i]['vis'])
                    _det = [self.frame_id, vis, ioa, detections[i]['gt_id']]

                    if detections[i]['gt_id'] in gt_available:
                        self.errors['unassigned_act_ioa_' + ioa] += 1
                        self.errors['unassigned_act_vis_' + vis] += 1

                        ind = gt_available.index(detections[i]['gt_id'])
                        tr_id = gt_available_k[ind]
                        ioa_gt = self.tracks[tr_id].ioa
                        vis_gt = self.tracks[tr_id].gt_vis
                        frame_gt = self.tracks[tr_id].im_index
                        id_gt = self.tracks[tr_id].gt_id
                        _gt = [frame_gt, vis_gt, ioa_gt, id_gt]
                        event = ['Unassigned', 'Act', dist[i, ind]] + _gt + _det
                        # if not detections[i]['gt_id'] == -1 or not id_gt == -1:
                        #     print(event)
                        event = [str(e) for e in event]
                        self.event_dict[self.seq][self.frame_id].append(event)

                    if detections[i]['gt_id'] in gt_available_b:
                        self.errors['unassigned_inact_ioa_' + ioa] += 1
                        self.errors['unassigned_inact_vis_' + vis] += 1

                        ind = gt_available_b.index(detections[i]['gt_id'])
                        tr_id = gt_available_bk[ind]
                        ioa_gt = self.inactive_tracks[tr_id].ioa
                        vis_gt = self.inactive_tracks[tr_id].gt_vis
                        frame_gt = self.inactive_tracks[tr_id].im_index
                        id_gt = self.inactive_tracks[tr_id].gt_id
                        _gt = [frame_gt, vis_gt, ioa_gt, id_gt]
                        event = ['Unassigned', 'Inact', dist[i, ind]] + _gt + _det
                        # if not detections[i]['gt_id'] == -1 or not id_gt == -1:
                        #     print(event)
                        event = [str(e) for e in event]
                        self.event_dict[self.seq][self.frame_id].append(event)

                self.tracks[self.id] = Track(track_id=self.id, **detections[i])
                tr_ids[i] = self.id
                self.id += 1
        return tr_ids

    def assign_act_inact_same_time(
            self,
            row,
            col,
            dist,
            detections,
            active_tracks,
            ids,
            tr_ids):
        # assigned contains all new detections that have been assigned
        assigned = list()
        act_thresh = 1000 if self.nan_first else self.act_reid_thresh
        inact_thresh = 1000 if self.nan_first else self.inact_reid_thresh

        for r, c in zip(row, col):
            # get reid threshold scale
            scale = max(0.4, (1-detections[r]['ioa'])**(1/10)) if \
                self.scale_thresh_ioa else 1

            # get detection information if debug
            if self.debug:
                ioa = self.round1(detections[r]['ioa'])
                vis = self.round1(detections[r]['vis'])
                _det = [self.frame_id, vis, ioa, detections[r]['gt_id']]

            # assign tracks to active tracks if reid distance < thresh
            if ids[c] in self.tracks.keys() and \
               (dist[r, c] < act_thresh * scale or self.nan_first):
                if self.tracker_cfg['active_proximity']:
                    if not self.proximity[r, c]:
                        continue
                
                # generate error event if debug
                if self.tracks[ids[c]].gt_id != detections[r]['gt_id'] \
                  and self.debug:
                    self.errors['wrong_assigned_act_ioa_' + ioa] += 1
                    self.errors['wrong_assigned_act_vis_' + vis] += 1

                    ioa_gt = self.tracks[ids[c]].ioa
                    vis_gt = self.tracks[ids[c]].gt_vis
                    frame_gt = self.tracks[ids[c]].im_index
                    id_gt = self.tracks[ids[c]].gt_id
                    _gt = [frame_gt, vis_gt, ioa_gt, id_gt]
                    event = ['WrongAssignment', 'Act', dist[r, c]] + _gt + _det
                    # if not detections[r]['gt_id'] == -1 or not id_gt == -1:
                    #     print(event)
                    event = [str(e) for e in event]
                    self.event_dict[self.seq][self.frame_id].append(event)
                self.tracks[ids[c]].add_detection(**detections[r])
                active_tracks.append(ids[c])
                assigned.append(r)
                tr_ids[r] = ids[c]

            # assign tracks to inactive tracks if reid distance < thresh
            elif ids[c] in self.inactive_tracks.keys() and \
               (dist[r, c] < inact_thresh * scale or self.nan_first):

                # generate error event if debug
                if self.inactive_tracks[ids[c]].gt_id != detections[r]['gt_id'] \
                  and self.debug:
                    self.errors['wrong_assigned_inact_ioa_' + ioa] += 1
                    self.errors['wrong_assigned_inact_vis_' + vis] += 1

                    ioa_gt = self.inactive_tracks[ids[c]].ioa
                    vis_gt = self.inactive_tracks[ids[c]].gt_vis
                    frame_gt = self.inactive_tracks[ids[c]].im_index
                    id_gt = self.inactive_tracks[ids[c]].gt_id
                    _gt = [frame_gt, vis_gt, ioa_gt, id_gt]
                    event = ['WrongAssignment', 'Inct', dist[r, c]] + _gt + _det
                    # if not detections[r]['gt_id'] == -1 or not id_gt == -1:
                    #     print(event)
                    event = [str(e) for e in event]
                    self.event_dict[self.seq][self.frame_id].append(event)

                # move inactive track to active
                self.tracks[ids[c]] = self.inactive_tracks[ids[c]]
                del self.inactive_tracks[ids[c]]
                self.tracks[ids[c]].inactive_count = 0

                self.tracks[ids[c]].add_detection(**detections[r])
                active_tracks.append(ids[c])
                assigned.append(r)
                tr_ids[r] = ids[c]

        return set(assigned)

    def assign_separatly(self, row, col, dist, detections, active_tracks, ids, tr_ids):
        # assign active tracks first
        assigned = self.assign_act_inact_same_time(
            row, col, dist[0], detections, active_tracks, ids[:dist[0].shape[1]], tr_ids)

        # assign inactive tracks
        if dist[1] is not None:
            # only use detections that have not been assigned yet
            unassigned = sorted(
                list(set(list(range(dist[0].shape[0]))) - assigned))

            if len(unassigned) != 0:
                dist[1] = dist[1][unassigned, :]

                row_inact, col_inact = solve_dense(dist[1])
                assigned_2 = self.assign_act_inact_same_time(
                    row=row_inact,
                    col=col_inact,
                    dist=dist[1],
                    detections=[t for i, t in enumerate(detections) if i in unassigned],
                    active_tracks=active_tracks,
                    ids=ids[dist[0].shape[1]:],
                    tr_ids=tr_ids)
                assigned_2 = set(
                    [u for i, u in enumerate(unassigned) if i in assigned_2])
                assigned.update(assigned_2)

        return assigned
