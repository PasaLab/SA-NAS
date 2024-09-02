import __init__
import os
import sys
import time
import glob
import numpy as np
import torch
from gcn import utils
import logging
import argparse
import torch.nn as nn
from torch_geometric.data import DataLoader
import torch.utils
import torch.backends.cudnn as cudnn
from model import NetworkPPI as Network
import genotypes
from torch_geometric.data import NeighborSampler
from torch_geometric.data.cluster import ClusterData, ClusterLoader
from ogb.nodeproppred import  PygNodePropPredDataset, Evaluator
# from torch_geometric.utils import index_to_mask
import sampler as GraphSAINT

parser = argparse.ArgumentParser("ogb")
parser.add_argument('--data', type=str, default='ogb-dataset/', help='location of the data corpus')
parser.add_argument('--phase', type=str, default='train', help='train/test')
parser.add_argument('--batch_size', type=int, default=1, help='batch size')
parser.add_argument('--learning_rate', type=float, default=0.003, help='init learning rate')
parser.add_argument('--momentum', type=float, default=0.9, help='momentum')
parser.add_argument('--weight_decay', type=float, default=3e-4, help='weight decay')
parser.add_argument('--report_freq', type=float, default=20, help='report frequency')
parser.add_argument('--gpu', type=int, default=2, help='gpu device id')
parser.add_argument('--epochs', type=int, default=200, help='num of training epochs')
parser.add_argument('--init_channels', type=int, default=100, help='num of init channels')
parser.add_argument('--num_cells', type=int, default=4, help='total number of cells')
parser.add_argument('--model_path', type=str, default='log/ckpt', help='path to save the model / pretrained')
parser.add_argument('--auxiliary', action='store_true', default=False, help='use auxiliary tower')
parser.add_argument('--auxiliary_weight', type=float, default=0.4, help='weight for auxiliary loss')
parser.add_argument('--cutout', action='store_true', default=False, help='use cutout')
parser.add_argument('--cutout_length', type=int, default=16, help='cutout length')
parser.add_argument('--drop_path_prob', type=float, default=0.3, help='drop path probability')
parser.add_argument('--save', type=str, default='EXP', help='experiment name')
parser.add_argument('--seed', type=int, default=19980813, help='random seed')
parser.add_argument('--arch', type=str, default='Products_Best', help='which architecture to use')
parser.add_argument('--grad_clip', type=float, default=10, help='gradient clipping')
parser.add_argument('--in_channels', default=128, type=int, help='the channel size of input')
args = parser.parse_args()
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

args.save = 'log/eval-{}-{}'.format(args.save, time.strftime("%Y%m%d-%H%M%S"))
utils.create_exp_dir(args.save, scripts_to_save=glob.glob('*.py'))

log_format = '%(asctime)s %(message)s'
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format=log_format, datefmt='%m/%d %I:%M:%S %p')
fh = logging.FileHandler(os.path.join(args.save, 'log.txt'))
fh.setFormatter(logging.Formatter(log_format))
logging.getLogger().addHandler(fh)


def train():
    best_val_acc = 0.
    best_test_acc = 0.
    for epoch in range(args.epochs):
        test_node = 0
        valid_node = 0
        logging.info('epoch %d lr %e', epoch, scheduler.get_lr()[0])
        model.drop_path_prob = args.drop_path_prob * epoch / args.epochs
        for batch in data_loader:
            acc_item = train_step(batch, model, criterion, optimizer)
            # true_node = acc * len(batch.x)

        for batch in data_loader:
            test_acc, valid_acc = infer(batch, model, evaluator, criterion)
            t_node = test_acc * len(batch.x[batch.test_mask])
            v_node = valid_acc * len(batch.x[batch.valid_mask])
            test_node += t_node
            valid_node += v_node

        test_acc = test_node / len(data.x[data.test_mask])
        valid_acc = valid_node / len(data.x[data.valid_mask])
        if valid_acc > best_val_acc:
            best_val_acc = valid_acc
            test_acc_when_best_val = test_acc
            utils.save(model, os.path.join(args.save, 'best_weights.pt'))
        if test_acc > best_test_acc:
            best_test_acc = test_acc

        logging.info('valid_acc %f\tbest_val_acc %f\ttest_acc %f\tbest_test_acc %f\tfinal_best_test %f',
                      valid_acc, best_val_acc, test_acc, best_test_acc, test_acc_when_best_val)

        utils.save(model, os.path.join(args.save, 'weights.pt'))
        scheduler.step()
    logging.info(
        'Finish! best_val_acc %f\t test_class_acc_when_best %f \t best test %f',
        best_test_acc, test_acc_when_best_val, best_test_acc)


def train_step(data, model, criterion, optimizer):
    model.train()
    data = data.to(DEVICE)
    target = data.y
    optimizer.zero_grad()
    out = model(data)[0]
    y_pred = out.argmax(dim=-1, keepdim=True)
    criterion = nn.CrossEntropyLoss()

    #pdb.set_trace()
    loss = criterion(out[data.train_mask], torch.squeeze(target[data.train_mask]))
    loss.backward()
    nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
    optimizer.step()
    
    # import pdb
    # pdb.set_trace()
    acc = evaluator.eval({
        'y_true': target[data.train_mask],
        'y_pred': y_pred[data.train_mask],
    })['acc']
    return acc

def index_to_mask(index, size):
    r"""Converts indices to a mask representation.

    Args:
        index (Tensor): The indices.
        size (int, optional): The size of the mask. If set to :obj:`None`, a
            minimal sized output mask is returned.

    Example:
        >>> index = torch.tensor([1, 3, 5])
        >>> index_to_mask(index)
        tensor([False,  True, False,  True, False,  True])

        >>> index_to_mask(index, size=7)
        tensor([False,  True, False,  True, False,  True, False])
    """
    index = index.view(-1)
    size = int(index.max()) + 1 if size is None else size
    mask = index.new_zeros(size, dtype=torch.bool)
    mask[index] = True
    return mask

def infer(data, model, evaluator,criterion):
    model.eval()
    data = data.to(DEVICE)
    out = model(data)[0]
    y_pred = out.argmax(dim=-1, keepdim=True)

    test_acc = evaluator.eval({
        'y_true': data.y[data.test_mask],
        'y_pred': y_pred[data.test_mask],
    })['acc']

    valid_acc = evaluator.eval({
        'y_true': data.y[data.valid_mask],
        'y_pred': y_pred[data.valid_mask],
    })['acc']
    return test_acc, valid_acc



if __name__ == '__main__':
    start = time.clock() 

    if not torch.cuda.is_available():
        logging.info('no gpu device available')
        sys.exit(1)
    
    np.random.seed(args.seed)
    torch.cuda.set_device(args.gpu)
    cudnn.benchmark = True
    torch.manual_seed(args.seed)
    cudnn.enabled = True
    torch.cuda.manual_seed(args.seed)
    logging.info('gpu device = %d' % args.gpu)
    logging.info("args = %s", args)


    dataset = PygNodePropPredDataset(name='ogbn-products', root=args.data)
    if dataset.name == 'ogbn-products':
        args.in_channels = 100
    data = dataset[0]
    split = dataset.get_idx_split()
    n_classes = dataset.num_classes

    genotype = eval("genotypes.%s" % args.arch)
    import pdb
    #pdb.set_trace()
    model = Network(args.init_channels, n_classes, args.num_cells, args.auxiliary, genotype,
                    in_channels=args.in_channels)
    model = model.cuda()

    logging.info("param size = %fMB", utils.count_parameters_in_MB(model))

    criterion = nn.CrossEntropyLoss().cuda()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, float(args.epochs))

    mask = torch.zeros(len(data.x), dtype=torch.bool)
    train_mask = torch.tensor(mask)
    valid_mask = torch.tensor(mask)
    test_mask = torch.tensor(mask)
    train_mask[split['train']] = True
    # train_mask[958748:973000] = True
    valid_mask[split['valid']] = True
    test_mask[split['test']] = True

    data.train_mask = train_mask
    data.valid_mask = valid_mask
    data.test_mask = test_mask
    n=len(data.y)
    # split_idx = dataset.get_idx_split()
    # data.train_mask = index_to_mask(split_idx['train'],
    #                                 size=data.num_nodes)
    # data.val_mask = index_to_mask(split_idx['valid'], size=data.num_nodes)
    # data.test_mask = index_to_mask(split_idx['test'], size=data.num_nodes)
    # data.y = data.y.squeeze()
    
    cluster_data = ClusterData(data, num_parts=15000, recursive=False)
    data_loader = ClusterLoader(cluster_data, batch_size=32, shuffle=True)
    # data_loader = GraphSAINT.GraphSAINTNodeSampler(data, batch_size=10000, num_steps=int(n/10000))

    evaluator = Evaluator('ogbn-products')
    if args.phase == 'test':
        #logging.info("===> Loading checkpoint '{}'".format(args.model_path))
        #utils.load(model, args.model_path)
        print(data)
        #sub_data = NeighborSampler(data, size=0.05, num_hops=2)
        #print(sub_data)
        #for batch in data_loader:
        #    print(batch.y)
        #    infer(batch, model, evaluator, criterion)
    else:
        train()
        end = time.clock()
        print('程序运行时间为: %s Seconds'%(end - start))

