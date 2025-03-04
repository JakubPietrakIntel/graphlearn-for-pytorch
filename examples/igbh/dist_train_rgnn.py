# Copyright 2023 Alibaba Group Holding Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import argparse, datetime
import os.path as osp
import time, tqdm

import graphlearn_torch as glt
import numpy as np
import sklearn.metrics
import torch
import torch.distributed
import torch.nn.functional as F

from torch.nn.parallel import DistributedDataParallel

from rgnn import RGNN


torch.manual_seed(42)


def evaluate(model, dataloader):
  predictions = []
  labels = []
  with torch.no_grad():
    for batch in dataloader:
      batch_size = batch['paper'].batch_size
      out = model(batch.x_dict, batch.edge_index_dict)[:batch_size]
      labels.append(batch['paper'].y[:batch_size].cpu().numpy())
      predictions.append(out.argmax(1).cpu().numpy())

    predictions = np.concatenate(predictions)
    labels = np.concatenate(labels)
    acc = sklearn.metrics.accuracy_score(labels, predictions)
    return acc

def run_training_proc(local_proc_rank, num_nodes, node_rank, num_training_procs,
    hidden_channels, num_classes, num_layers, model_type, num_heads, fan_out,
    epochs, batch_size, learning_rate, log_every,
    dataset, train_idx, val_idx, test_idx,
    master_addr,
    training_pg_master_port,
    train_loader_master_port,
    val_loader_master_port,
    test_loader_master_port,
    with_gpu,
    rpc_timeout):
  # Initialize graphlearn_torch distributed worker group context.
  glt.distributed.init_worker_group(
    world_size=num_nodes*num_training_procs,
    rank=node_rank*num_training_procs+local_proc_rank,
    group_name='distributed-igbh-trainer'
  )

  current_ctx = glt.distributed.get_context()
  if with_gpu:
    current_device = torch.device(local_proc_rank % torch.cuda.device_count())
  else:
    current_device = torch.device('cpu')

  # Initialize training process group of PyTorch.
  torch.distributed.init_process_group(
    backend='nccl' if with_gpu else 'gloo',
    rank=current_ctx.rank,
    world_size=current_ctx.world_size,
    init_method='tcp://{}:{}'.format(master_addr, training_pg_master_port)
  )

  # Create distributed neighbor loader for training
  train_idx = train_idx.split(train_idx.size(0) // num_training_procs)[local_proc_rank]
  train_loader = glt.distributed.DistNeighborLoader(
    data=dataset,
    num_neighbors=[int(fanout) for fanout in fan_out.split(',')],
    input_nodes=('paper', train_idx),
    batch_size=batch_size,
    shuffle=True,
    drop_last=False,
    collect_features=True,
    to_device=current_device,
    worker_options=glt.distributed.MpDistSamplingWorkerOptions(
      num_workers=1,
      worker_devices=[current_device] if with_gpu else [torch.device('cpu')],
      worker_concurrency=2,
      master_addr=master_addr,
      master_port=train_loader_master_port,
      channel_size='2GB',
      pin_memory=True if with_gpu else False,
      rpc_timeout=rpc_timeout,
    )
  )
  # Create distributed neighbor loader for validation.
  val_idx = val_idx.split(val_idx.size(0) // num_training_procs)[local_proc_rank]
  val_loader = glt.distributed.DistNeighborLoader(
    data=dataset,
    num_neighbors=[int(fanout) for fanout in fan_out.split(',')],
    input_nodes=('paper', val_idx),
    batch_size=batch_size,
    shuffle=False,
    collect_features=True,
    to_device=current_device,
    worker_options = glt.distributed.CollocatedDistSamplingWorkerOptions(
      master_addr=master_addr,
      master_port=val_loader_master_port,
      rpc_timeout=rpc_timeout,
    )
  )

  # Create distributed neighbor loader for testing.
  test_idx = test_idx.split(test_idx.size(0) // num_training_procs)[local_proc_rank]
  test_loader = glt.distributed.DistNeighborLoader(
    data=dataset,
    num_neighbors=[int(fanout) for fanout in fan_out.split(',')],
    input_nodes=('paper', test_idx),
    batch_size=batch_size,
    shuffle=False,
    collect_features=True,
    to_device=current_device,
    worker_options = glt.distributed.CollocatedDistSamplingWorkerOptions(
      master_addr=master_addr,
      master_port=test_loader_master_port,
      rpc_timeout=rpc_timeout,
    )
  )

  # Define model and optimizer.
  if with_gpu:
    torch.cuda.set_device(current_device)
  model = RGNN(dataset.get_edge_types(),
               dataset.node_features['paper'].shape[1],
               hidden_channels,
               num_classes,
               num_layers=num_layers,
               dropout=0.2,
               model=model_type,
               heads=num_heads,
               node_type='paper').to(current_device)
  model = DistributedDataParallel(model,
                                  device_ids=[current_device.index] if with_gpu else None,
                                  find_unused_parameters=True)

  param_size = 0
  for param in model.parameters():
    param_size += param.nelement() * param.element_size()
  buffer_size = 0
  for buffer in model.buffers():
    buffer_size += buffer.nelement() * buffer.element_size()

  size_all_mb = (param_size + buffer_size) / 1024**2
  print('model size: {:.3f}MB'.format(size_all_mb))

  loss_fcn = torch.nn.CrossEntropyLoss().to(current_device)
  optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

  best_accuracy = 0
  training_start = time.time()
  for epoch in tqdm.tqdm(range(epochs)):
    model.train()
    total_loss = 0
    train_acc = 0
    idx = 0
    gpu_mem_alloc = 0
    epoch_start = time.time()
    for batch in train_loader:
      idx += 1
      batch_size = batch['paper'].batch_size
      out = model(batch.x_dict, batch.edge_index_dict)[:batch_size]
      y = batch['paper'].y[:batch_size]
      loss = loss_fcn(out, y)
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()
      total_loss += loss.item()
      train_acc += sklearn.metrics.accuracy_score(y.cpu().numpy(),
          out.argmax(1).detach().cpu().numpy())*100
      gpu_mem_alloc += (
          torch.cuda.max_memory_allocated() / 1000000
          if with_gpu
          else 0
      )
    train_acc /= idx
    gpu_mem_alloc /= idx
    if with_gpu:
      torch.cuda.synchronize()
      torch.distributed.barrier()
    if epoch%log_every == 0:
      model.eval()
      val_acc = evaluate(model, val_loader).item()*100
      if best_accuracy < val_acc:
        best_accuracy = val_acc
      if with_gpu:
        torch.cuda.synchronize()
        torch.distributed.barrier()
      tqdm.tqdm.write(
          "Rank{:02d} | Epoch {:03d} | Loss {:.4f} | Train Acc {:.2f} | Val Acc {:.2f} | Time {} | GPU {:.1f} MB".format(
              current_ctx.rank,
              epoch,
              total_loss,
              train_acc,
              val_acc,
              str(datetime.timedelta(seconds = int(time.time() - epoch_start))),
              gpu_mem_alloc
          )
      )

  model.eval()
  test_acc = evaluate(model, test_loader).item()*100
  print("Rank {:02d} Test Acc {:.2f}%".format(current_ctx.rank, test_acc))
  print("Total time taken " + str(datetime.timedelta(seconds = int(time.time() - training_start))))


if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  root = osp.join(osp.dirname(osp.dirname(osp.dirname(osp.realpath(__file__)))), 'data', 'igbh')
  glt.utils.ensure_dir(root)
  parser.add_argument('--path', type=str, default=root,
      help='path containing the datasets')
  parser.add_argument('--dataset_size', type=str, default='tiny',
      choices=['tiny', 'small', 'medium', 'large', 'full'],
      help='size of the datasets')
  parser.add_argument('--num_classes', type=int, default=19,
      choices=[19, 2983], help='number of classes')
  parser.add_argument('--in_memory', type=int, default=0,
      choices=[0, 1], help='0:read only mmap_mode=r, 1:load into memory')
  # Model
  parser.add_argument('--model', type=str, default='rgat',
                      choices=['rgat', 'rsage'])
  # Model parameters
  parser.add_argument('--fan_out', type=str, default='10,15')
  parser.add_argument('--batch_size', type=int, default=512)
  parser.add_argument('--hidden_channels', type=int, default=128)
  parser.add_argument('--learning_rate', type=int, default=0.001)
  parser.add_argument('--epochs', type=int, default=20)
  parser.add_argument('--num_layers', type=int, default=6)
  parser.add_argument('--num_heads', type=int, default=4)
  parser.add_argument('--log_every', type=int, default=5)
  # Distributed settings.
  parser.add_argument("--num_nodes", type=int, default=2,
      help="Number of distributed nodes.")
  parser.add_argument("--node_rank", type=int, default=0,
      help="The current node rank.")
  parser.add_argument("--num_training_procs", type=int, default=2,
      help="The number of traning processes per node.")
  parser.add_argument("--master_addr", type=str, default='localhost',
      help="The master address for RPC initialization.")
  parser.add_argument("--training_pg_master_port", type=int, default=11111,
      help="The port used for PyTorch's process group initialization across training processes.")
  parser.add_argument("--train_loader_master_port", type=int, default=11112,
      help="The port used for RPC initialization across all sampling workers of train loader.")
  parser.add_argument("--val_loader_master_port", type=int, default=11113,
      help="The port used for RPC initialization across all sampling workers of val loader.")
  parser.add_argument("--test_loader_master_port", type=int, default=11114,
      help="The port used for RPC initialization across all sampling workers of test loader.")
  parser.add_argument("--cpu_mode", action="store_true",
      help="Only use CPU for sampling and training, default is False.")
  parser.add_argument("--rpc_timeout", type=int, default=180,
                      help="rpc timeout in seconds")
  args = parser.parse_args()
  # when set --cpu_mode or GPU is not available, use cpu only mode.
  args.with_gpu = (not args.cpu_mode) and torch.cuda.is_available()
  if args.with_gpu:
    assert(not args.num_training_procs > torch.cuda.device_count())

  print('--- Loading data partition ...\n')
  data_pidx = args.node_rank % args.num_nodes
  dataset = glt.distributed.DistDataset()
  dataset.load(
    root_dir=osp.join(args.path, f'{args.dataset_size}-partitions'),
    partition_idx=data_pidx,
    graph_mode='ZERO_COPY' if args.with_gpu else 'CPU',
    feature_with_gpu=args.with_gpu,
    whole_node_label_file={'paper': osp.join(args.path, f'{args.dataset_size}-label', 'label.pt')}
  )
  train_idx = torch.load(
    osp.join(args.path, f'{args.dataset_size}-train-partitions', f'partition{data_pidx}.pt')
  )
  val_idx = torch.load(
    osp.join(args.path, f'{args.dataset_size}-val-partitions', f'partition{data_pidx}.pt')
  )
  test_idx = torch.load(
    osp.join(args.path, f'{args.dataset_size}-test-partitions', f'partition{data_pidx}.pt')
  )
  train_idx.share_memory_()
  val_idx.share_memory_()
  test_idx.share_memory_()

  print('--- Launching training processes ...\n')
  torch.multiprocessing.spawn(
    run_training_proc,
    args=(args.num_nodes, args.node_rank, args.num_training_procs,
          args.hidden_channels, args.num_classes, args.num_layers, args.model, args.num_heads, args.fan_out,
          args.epochs, args.batch_size, args.learning_rate, args.log_every,
          dataset, train_idx, val_idx, test_idx,
          args.master_addr,
          args.training_pg_master_port,
          args.train_loader_master_port,
          args.val_loader_master_port,
          args.test_loader_master_port,
          args.with_gpu,
          args.rpc_timeout),
    nprocs=args.num_training_procs,
    join=True
  )
