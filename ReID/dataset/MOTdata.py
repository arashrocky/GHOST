from __future__ import absolute_import
from __future__ import print_function
from __future__ import division
#from _typeshed import SupportsLessThan

import sys
import os
import os.path as osp

from torchreid.data import ImageDataset, VideoDataset
from skimage.io import imread, imsave
#from skimage.util import pad
import copy
import tqdm

import pandas as pd
import numpy as np
import json

def read_json(path):
    with open(path) as json_file:
        data = json.load(json_file)
    return data

def anns2df(anns, img_dir):
    # Build DF from anns
    to_kps = lambda x: np.array(x['keypoints']).reshape(-1, 3)
    rows = []
    for ann in tqdm.tqdm(anns['annotations']):
        row={'path': f"{img_dir}/{ann['id']}.png",
            'model_id': int(ann['model_id']),
            'height': int(ann['bbox'][-1]),
            'width': int(ann['bbox'][-2]),
            'iscrowd': int(ann['iscrowd']),
            'isnight': int(ann['is_night']),
            'vis' : (to_kps(ann)[..., 2] ==2).mean(),
            'frame_n': int(ann['frame_n']),
            **{f'attr_{i}': int(attr_val) for i, attr_val in enumerate(ann['attributes'])}}
        rows.append(row)

    return  pd.DataFrame(rows)

def anns2df_motcha(anns, img_dir):
    # Build DF from anns
    rows = []
    for ann in tqdm.tqdm(anns['annotations']):
        row={'path': f"{osp.join(img_dir)}/{ann['id']}.png",
            'ped_id': int(ann['ped_id']),
            'height': int(ann['bbox'][-1]),
            'width': int(ann['bbox'][-2]),
            'iscrowd': int(ann['iscrowd']),
            'vis' : float(ann['vis']),
            'frame_n': int(ann['frame_n'])}
        rows.append(row)

    return  pd.DataFrame(rows)

def assign_ids(df, night_id=True, attr_indices = [0, 2, 3, 4, 7, 8, 9, 10]):
    id_cols = ['model_id'] + [f'attr_{i}' for i in attr_indices if f'attr{i}' in df.columns] 
    if night_id and 'isnight' in df.columns:
        id_cols += ['isnight']

    unique_ids_df = df[id_cols].drop_duplicates()
    unique_ids_df['reid_id'] = np.arange(unique_ids_df.shape[0])

    return  df.merge(unique_ids_df)

def clean_rows(df, min_vis, min_h, min_w, min_samples):
    # Filter by size and occlusion
    keep = (df['vis'] >= min_vis) & (df['height']>=min_h) & (df['width'] >= min_w) & (df['iscrowd']==0)
    clean_df = df[keep]
    # Keep only ids with at least MIN_SAMPLES appearances
    clean_df['samples_per_id'] = clean_df.groupby('reid_id')['height'].transform('count').values
    clean_df = clean_df[clean_df['samples_per_id']>=min_samples]

    return clean_df

def relabel_ids(df):
    df.rename(columns = {'reid_id': 'reid_id_old'}, inplace=True)
    df['old_id_seq'] = [seq + "_" + str(i) for seq, i in zip(df['Sequence'].values, df['reid_id_old'].values)]

    # Relabel Ids from 0 to N-1
    ids_df = df[['old_id_seq']].drop_duplicates()
    ids_df['reid_id'] = np.arange(ids_df.shape[0])
    df = df.merge(ids_df)

    return df

class MOTSeqDataset(ImageDataset):
    def __init__(self,ann_files, img_dir, min_vis=0.3, min_h=50, min_w=25, \
            min_samples=15, night_id=True, motcha=False, split='split_1', \
            seq_names=None, **kwargs):

        for i, (ann_file, seq_name) in enumerate(zip(ann_files, seq_names)):
            # Create a Pandas DataFrame out of json annotations file
            print("Reading json...")
            anns = read_json(ann_file)
            print("Done!")
            
            print("Preparing dataset...")
            if motcha:
                df = anns2df_motcha(anns, img_dir)
                df['reid_id'] = df['ped_id']

            else:
                df = anns2df(anns, img_dir)
                df = assign_ids(df, night_id=True, attr_indices = [0, 2, 3, 4, 7, 8, 9, 10])

            df= clean_rows(df, min_vis, min_h=min_h, min_w=min_w, min_samples=min_samples)
            df['Sequence'] = seq_name
            if i == 0:
                df_all = df
            else:
                df_all = df_all.append(df)
            
        df_all = relabel_ids(df_all)

        # For testing, choose one apperance randomly for every track and put in the gallery
        to_tuple_list = lambda df: list(df[['path', 'reid_id', 'cam_id']].itertuples(\
            index=False, name=None))
        df['cam_id'] = 0
        train = to_tuple_list(df)

        df['index'] = df.index.values
        np.random.seed(0)
        query_per_id = df.groupby('reid_id')['index'].agg(lambda x: np.random.choice(list(x.unique())))
        query_df = df.loc[query_per_id.values].copy()
        gallery_df = df.drop(query_per_id).copy()
        
        # IMPORTANT: For testing, torchreid only compares gallery and query images from different cam_ids
        # therefore we just assign them ids 0 and 1 respectively
        gallery_df['cam_id'] = 1
        
        query=to_tuple_list(query_df)
        gallery=to_tuple_list(gallery_df)

        print("Done!")
        super(MOTSeqDataset, self).__init__(train, query, gallery, **kwargs)
        

def get_sequence_class(seq_names, split='split_1'):
    
    # raise RuntimeError("MODify this path to wherever you store json annotations and reid data!!")
    ann_files = [f'/storage/remote/atcremers82/mot_neural_solver/sanity_check_data/red_annotations/MOT17_{seq_name}.json' for seq_name in seq_names]
    img_dir = '/storage/remote/atcremers82/mot_neural_solver/sanity_check_data/reid'
    min_samples = 5
    motcha=True

    
    class MOTSpecificSeq(MOTSeqDataset):
        def __init__(self, **kwargs):
            super(MOTSpecificSeq, self).__init__(ann_files=ann_files, img_dir=img_dir, \
                min_samples=min_samples, motcha=motcha, split=split, seq_names=seq_names, \
                    **kwargs)

    MOTSpecificSeq.__name__=split
    
    return MOTSpecificSeq
        


if __name__ == '__main__':
    dataset = get_sequence_class(["MOT17-02", "MOT17-04", "MOT17-05", "MOT17-09", "MOT17-10", "MOT17-11", "MOT17-13"])
    dataset = dataset()