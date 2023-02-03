import yaml
import argparse
import torch
import logging
import warnings
import time
import os.path as osp
import os
import utils.utils as utils
from utils.trainer import Trainer 


logger = logging.getLogger('GNNReID')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(message)s')

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(formatter)

fh = logging.FileHandler('train_reid.txt')
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)

logger.addHandler(ch)
logger.addHandler(fh)

warnings.filterwarnings("ignore")


def init_args():
    parser = argparse.ArgumentParser(description='Person Re-ID with for MOT')
    parser.add_argument('--config_path', type=str, default='config/config_MOT.yaml', help='Path to config file')

    return parser.parse_args() 


def main(args):
    with open(args.config_path, 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    save_folder_nets = 'search_results_net'
    utils.make_dir(save_folder_nets)
    
    splits = ['split_1', 'split_2', 'split_3']
    
    for s in splits:
        config['dataset']['split'] = s
        trainer = Trainer(config, save_folder_nets, device,
                        timer=time.time())
        trainer.train()


if __name__ == '__main__':
    args = init_args()
    main(args)