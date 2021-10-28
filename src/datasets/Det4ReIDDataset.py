from collections import defaultdict
import os
import random
from .MOTDataset import MOTDataset
from .MOT17_parser import MOTLoader
import pandas as pd
import PIL.Image as Image
from ReID.dataset.utils import make_transform_whole_img
import copy
import numpy as np

def collate(batch):
    data = [item[0] for item in batch]
    target = [item[1] for item in batch]
    visibility = [item[2] for item in batch]
    im_path = [item[3] for item in batch]
    dets = [item[4] for item in batch]

    return [data, target, visibility, im_path, dets]


class Det4ReIDDataset(MOTDataset):
    def __init__(self, split, sequences, dataset_cfg, tracker_cfg, dir, datastorage='data', augment=False, train=False):
        self.mode = split.split('_')[-1]
        self.data = list()
        self.data_unclipped = list()
        self.gt = list()
        self.augment = augment
        self.train = train
        if split[-5:] == 'along':
            self.split_along_seq = True
        else:
            self.split_along_seq = False

        # if dist was already computed for this seq
        super(Det4ReIDDataset, self).__init__(split, sequences, dataset_cfg, dir, datastorage, add_detector=False)
        self.transform = make_transform_whole_img()

    def process(self):
        self.id_to_y = dict()

        for seq in self.sequences:
            #print(seq)
            seq_ids = dict()
            if not self.preprocessed_exists or self.dataset_cfg['prepro_again']:
                loader = MOTLoader([seq], self.dataset_cfg, self.dir)
                loader.get_seqs()
                
                dets = loader.dets
                os.makedirs(self.preprocessed_dir, exist_ok=True)
                dets.to_pickle(self.preprocessed_paths[seq])

                dets_unclipped = loader.dets_unclipped
                dets_unclipped.to_pickle(self.preprocessed_paths[seq][:-4] + '_unclipped.pkl')

                gt = loader.gt
                os.makedirs(self.preprocessed_dir, exist_ok=True)
                gt.to_pickle(self.preprocessed_gt_paths[seq])
            else:
                dets = pd.read_pickle(self.preprocessed_paths[seq])
                dets_unclipped = pd.read_pickle(self.preprocessed_paths[seq][:-4] + '_unclipped.pkl')

            dets['gt_id'] = copy.deepcopy(dets['id'].values)

            for i, row in dets.iterrows():
                if row['id'] not in seq_ids.keys():
                    seq_ids[row['id']] = self.id
                    self.id += 1
                dets.at[i, 'id'] = seq_ids[row['id']]
                dets_unclipped.at[i, 'id'] = seq_ids[row['id']]

            self.id_to_y[seq] = seq_ids

            self.data.append(dets)
            self.data_unclipped.append(dets_unclipped)
            self.gt.append(gt)

        self.id += 1
        self.seperate_seqs()

    def seperate_seqs(self):
        frames = list()
        frames_gt = list()
        ys_list = list()
        seq_frame_list = defaultdict(int)
        for i, seq in enumerate(self.data):
            seq['Sequence'] = len(seq) * [self.sequences[i]]
            for frame in sorted(seq['frame'].unique()):
                if self.split_along_seq:
                    if self.train and frame >= sorted(seq['frame'].unique())[-1] * 2/3:
                        break
                    if not self.train and frame < sorted(seq['frame'].unique())[-1] * 2/3:
                        continue
                frame_df = seq[seq['frame']==frame]
                ys_list.append(frame_df['id'].values)
                frames.append(frame_df)
                frames_gt.append(self.gt[i][self.gt[i]['frame']==frame])
                seq_frame_list[self.sequences[i]] += 1
            
        self.data = frames
        self.ys_list = ys_list
        self.gt_all = frames_gt
        print(seq_frame_list)

    def augment_boxes(self, boxes):
        # change size of bb
        size_changes = np.asarray([[random.uniform(-0.05, 0.05) \
            for _ in range(4)] for _ in range(boxes.shape[0])])
        boxes = boxes + boxes*size_changes

        # move bb
        pos_changes = np.asarray([[random.uniform(-0.05, 0.05)] * 4 \
            for _ in range(boxes.shape[0])])
        boxes = boxes + boxes * pos_changes
        
        return boxes

    def get_bounding_box(self, bbs, bbs_gt):
        # tracktor resize (256,128))

        img = self.to_tensor(Image.open(bbs.iloc[0]['frame_path']).convert("RGB"))
        img = self.to_pil(img)
        img = self.transform(img)
        gt_boxes = bbs_gt[['bb_left', 'bb_top', 'bb_right', 'bb_bot']].values
        boxes = bbs[['bb_left', 'bb_top', 'bb_right', 'bb_bot']].values
        ids = bbs['id'].values

        if self.augment:
            self.augment_boxes(boxes)

        return img, {'boxes': gt_boxes, 'public_preds': boxes, 'ids': ids}
 
    def __getitem__(self, idx, train=True):
        bbs = self.data[idx]
        bbs_gt = self.gt_all[idx]
        img, labels = self.get_bounding_box(bbs, bbs_gt)

        return img, labels

    def num_classes(self):
        return self.id

