# Copyright 2022 Alibaba Group Holding Limited. All Rights Reserved.
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

import os
import pickle
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Union

import torch

from ..typing import (
  NodeType, EdgeType, as_str, TensorDataType,
  GraphPartitionData, HeteroGraphPartitionData,
  FeaturePartitionData, HeteroFeaturePartitionData,
  PartitionBook, HeteroNodePartitionDict, HeteroEdgePartitionDict
)
from ..utils import convert_to_tensor, ensure_dir, id2idx


def save_meta(
  output_dir: str,
  num_parts: int,
  data_cls: str = 'homo',
  node_types: Optional[List[NodeType]] = None,
  edge_types: Optional[List[EdgeType]] = None,
):
  r""" Save partitioning meta info into the output directory.
  """
  meta = {
    'num_parts': num_parts,
    'data_cls': data_cls,
    'node_types': node_types,
    'edge_types': edge_types
  }
  with open(os.path.join(output_dir, 'META'), 'wb') as outfile:
    pickle.dump(meta, outfile, pickle.HIGHEST_PROTOCOL)


def save_node_pb(
  output_dir: str,
  node_pb: PartitionBook,
  ntype: Optional[NodeType] = None
):
  r""" Save a partition book of graph nodes into the output directory.
  """
  if ntype is not None:
    subdir = os.path.join(output_dir, 'node_pb')
    ensure_dir(subdir)
    fpath = os.path.join(subdir, f'{as_str(ntype)}.pt')
  else:
    fpath = os.path.join(output_dir, 'node_pb.pt')
  torch.save(node_pb, fpath)


def save_edge_pb(
  output_dir: str,
  edge_pb: PartitionBook,
  etype: Optional[EdgeType] = None
):
  r""" Save a partition book of graph edges into the output directory.
  """
  if etype is not None:
    subdir = os.path.join(output_dir, 'edge_pb')
    ensure_dir(subdir)
    fpath = os.path.join(subdir, f'{as_str(etype)}.pt')
  else:
    fpath = os.path.join(output_dir, 'edge_pb.pt')
  torch.save(edge_pb, fpath)


def save_graph_partition(
  output_dir: str,
  partition_idx: int,
  graph_partition: GraphPartitionData,
  etype: Optional[EdgeType] = None
):
  r""" Save a graph topology partition into the output directory.
  """
  subdir = os.path.join(output_dir, f'part{partition_idx}', 'graph')
  if etype is not None:
    subdir = os.path.join(subdir, as_str(etype))
  ensure_dir(subdir)
  torch.save(graph_partition.edge_index[0], os.path.join(subdir, 'rows.pt'))
  torch.save(graph_partition.edge_index[1], os.path.join(subdir, 'cols.pt'))
  torch.save(graph_partition.eids, os.path.join(subdir, 'eids.pt'))


def save_feature_partition(
  output_dir: str,
  partition_idx: int,
  feature_partition: FeaturePartitionData,
  group: str = 'node_feat',
  graph_type: Optional[Union[NodeType, EdgeType]] = None
):
  r""" Save a feature partition into the output directory.
  """
  subdir = os.path.join(output_dir, f'part{partition_idx}', group)
  if graph_type is not None:
    subdir = os.path.join(subdir, as_str(graph_type))
  ensure_dir(subdir)
  torch.save(feature_partition.feats, os.path.join(subdir, 'feats.pt'))
  torch.save(feature_partition.ids, os.path.join(subdir, 'ids.pt'))
  if feature_partition.cache_feats is not None:
    torch.save(feature_partition.cache_feats, os.path.join(subdir, 'cache_feats.pt'))
    torch.save(feature_partition.cache_ids, os.path.join(subdir, 'cache_ids.pt'))


class PartitionerBase(ABC):
  r""" Base class for partitioning graphs and features.
  """
  def __init__(
    self,
    output_dir: str,
    num_parts: int,
    num_nodes: Union[int, Dict[NodeType, int]],
    edge_index: Union[TensorDataType, Dict[EdgeType, TensorDataType]],
    node_feat: Optional[Union[TensorDataType, Dict[NodeType, TensorDataType]]] = None,
    node_feat_dtype: torch.dtype = torch.float32,
    edge_feat: Optional[Union[TensorDataType, Dict[EdgeType, TensorDataType]]] = None,
    edge_feat_dtype: torch.dtype = torch.float32,
    edge_assign_strategy: str = 'by_src',
    chunk_size: int = 10000,
  ):
    self.output_dir = output_dir
    ensure_dir(self.output_dir)

    self.num_parts = num_parts
    assert self.num_parts > 1

    self.num_nodes = num_nodes
    self.edge_index = convert_to_tensor(edge_index, dtype=torch.int64)
    self.node_feat = convert_to_tensor(node_feat, dtype=node_feat_dtype)
    self.edge_feat = convert_to_tensor(edge_feat, dtype=edge_feat_dtype)

    if isinstance(self.num_nodes, dict):
      assert isinstance(self.edge_index, dict)
      assert isinstance(self.node_feat, dict) or self.node_feat is None
      assert isinstance(self.edge_feat, dict) or self.edge_feat is None
      self.data_cls = 'hetero'
      self.node_types = list(self.num_nodes.keys())
      self.edge_types = list(self.edge_index.keys())
      self.num_edges = {}
      for etype, index in self.edge_index.items():
        self.num_edges[etype] = len(index[0])
    else:
      self.data_cls = 'homo'
      self.node_types = None
      self.edge_types = None
      self.num_edges = len(self.edge_index[0])

    self.edge_assign_strategy = edge_assign_strategy.lower()
    assert self.edge_assign_strategy in ['by_src', 'by_dst']
    self.chunk_size = chunk_size

  def get_edge_index(self, etype: Optional[EdgeType] = None):
    if 'hetero' == self.data_cls:
      assert etype is not None
      return self.edge_index[etype]
    return self.edge_index

  def get_node_feat(self, ntype: Optional[NodeType] = None):
    if self.node_feat is None:
      return None
    if 'hetero' == self.data_cls:
      assert ntype is not None
      return self.node_feat[ntype]
    return self.node_feat

  def get_edge_feat(self, etype: Optional[EdgeType] = None):
    if self.edge_feat is None:
      return None
    if 'hetero' == self.data_cls:
      assert etype is not None
      return self.edge_feat[etype]
    return self.edge_feat

  @abstractmethod
  def _partition_node(
    self,
    ntype: Optional[NodeType] = None
  ) -> Tuple[List[torch.Tensor], PartitionBook]:
    r""" Partition graph nodes of a specify node type, needs to be overwritten.

    Args:
      ntype (str): The type for input nodes, must be provided for heterogeneous
        graph. (default: ``None``)

    Returns:
      List[torch.Tensor]: The list of partitioned nodes ids.
      PartitionBook: The partition book of graph nodes.
    """

  @abstractmethod
  def _cache_node(
    self,
    ntype: Optional[NodeType] = None
  ) -> List[Optional[torch.Tensor]]:
    r""" Do feature caching and get cached results of a specify
    node type, needs to be overwritten.

    Returns:
      List[Optional[torch.Tensor]]: list of node ids need to be cached on
        each partition.
    """

  def _partition_graph(
    self,
    node_pb: Union[PartitionBook, Dict[NodeType, PartitionBook]],
    etype: Optional[EdgeType] = None
  ) -> Tuple[List[GraphPartitionData], PartitionBook]:
    r""" Partition graph topology of a specify edge type, needs to be
      overwritten.

    Args:
      node_pb (PartitionBook or Dict[NodeType, PartitionBook]): The partition
        books of graph nodes.
      etype (Tuple[str, str, str]): The type for input edges, must be provided
        for heterogeneous graph. (default: ``None``)

    Returns:
      List[GraphPartitionData]: A list of graph data for each partition.
      PartitionBook: The partition book of graph edges.
    """
    edge_index = self.get_edge_index(etype)
    rows, cols = edge_index[0], edge_index[1]
    edge_num = len(rows)
    eids = torch.arange(edge_num, dtype=torch.int64)

    if 'hetero' == self.data_cls:
      assert etype is not None
      assert isinstance(node_pb, dict)
      src_ntype, _, dst_ntype = etype

      if 'by_src' == self.edge_assign_strategy:
        target_node_pb = node_pb[src_ntype]
        target_indices = rows
      else:
        target_node_pb = node_pb[dst_ntype]
        target_indices = cols
    else:
      target_node_pb = node_pb
      target_indices = rows if 'by_src' == self.edge_assign_strategy else cols

    chunk_num = (edge_num + self.chunk_size - 1) // self.chunk_size
    chunk_start_pos = 0
    res = [[] for _ in range(self.num_parts)]
    for _ in range(chunk_num):
      chunk_end_pos = min(edge_num, chunk_start_pos + self.chunk_size)
      current_chunk_size = chunk_end_pos - chunk_start_pos
      chunk_idx = torch.arange(current_chunk_size, dtype=torch.long)
      chunk_rows = rows[chunk_start_pos:chunk_end_pos]
      chunk_cols = cols[chunk_start_pos:chunk_end_pos]
      chunk_eids = eids[chunk_start_pos:chunk_end_pos]

      chunk_target_indices = target_indices[chunk_start_pos:chunk_end_pos]
      chunk_partition_idx = target_node_pb[chunk_target_indices]
      for pidx in range(self.num_parts):
        mask = (chunk_partition_idx == pidx)
        idx = torch.masked_select(chunk_idx, mask)
        res[pidx].append(GraphPartitionData(
          edge_index=(chunk_rows[idx], chunk_cols[idx]),
          eids=chunk_eids[idx]
        ))
      chunk_start_pos += current_chunk_size

    partition_book = torch.zeros(edge_num, dtype=torch.long)
    partition_results = []
    for pidx in range(self.num_parts):
      p_rows = torch.cat([r.edge_index[0] for r in res[pidx]])
      p_cols = torch.cat([r.edge_index[1] for r in res[pidx]])
      p_eids = torch.cat([r.eids for r in res[pidx]])
      partition_book[p_eids] = pidx
      partition_results.append(GraphPartitionData(
        edge_index=(p_rows, p_cols),
        eids=p_eids
      ))

    return partition_results, partition_book

  def _partition_node_feat(
    self,
    node_ids_list: List[torch.Tensor],
    ntype: Optional[NodeType] = None,
  ) -> List[FeaturePartitionData]:
    r""" Partition node features by the partitioned node results, and calculate
    the cached nodes if needed.
    """
    node_feat = self.get_node_feat(ntype)
    if node_feat is None:
      return [None for _ in range(self.num_parts)]
    cache_node_ids_list = self._cache_node(ntype)
    res = []
    for pidx in range(self.num_parts):
      n_ids = node_ids_list[pidx]
      cache_n_ids = cache_node_ids_list[pidx]
      p_node_feat = FeaturePartitionData(
        feats=node_feat[n_ids],
        ids=n_ids,
        cache_feats=(node_feat[cache_n_ids] if cache_n_ids is not None else None),
        cache_ids=cache_n_ids
      )
      res.append(p_node_feat)
    return res

  def _partition_edge_feat(
    self,
    graph_list: List[GraphPartitionData],
    etype: Optional[EdgeType] = None
  ) -> List[FeaturePartitionData]:
    r""" Partition edge features by the partitioned edge results.
    """
    edge_feat = self.get_edge_feat(etype)
    if edge_feat is None:
      return [None for _ in range(self.num_parts)]
    res = []
    for pidx in range(self.num_parts):
      eids = graph_list[pidx].eids
      p_edge_feat = FeaturePartitionData(
        feats=edge_feat[eids], ids=eids,
        cache_feats=None, cache_ids=None
      )
      res.append(p_edge_feat)
    return res

  def partition(self):
    r""" Partition graph and feature data into different parts.

    The output directory of partitioned graph data will be like:

    * homogeneous

      root_dir/
      |-- META
      |-- node_pb.pt
      |-- edge_pb.pt
      |-- part0/
          |-- graph/
              |-- rows.pt
              |-- cols.pt
              |-- eids.pt
          |-- node_feat/
              |-- feats.pt
              |-- ids.pt
              |-- cache_feats.pt (optional)
              |-- cache_ids.pt (optional)
          |-- edge_feat/
              |-- feats.pt
              |-- ids.pt
              |-- cache_feats.pt (optional)
              |-- cache_ids.pt (optional)
      |-- part1/
          |-- graph/
              ...
          |-- node_feat/
              ...
          |-- edge_feat/
              ...

    * heterogeneous

      root_dir/
      |-- META
      |-- node_pb/
          |-- ntype1.pt
          |-- ntype2.pt
      |-- edge_pb/
          |-- etype1.pt
          |-- etype2.pt
      |-- part0/
          |-- graph/
              |-- etype1/
                  |-- rows.pt
                  |-- cols.pt
                  |-- eids.pt
              |-- etype2/
                  ...
          |-- node_feat/
              |-- ntype1/
                  |-- feats.pt
                  |-- ids.pt
                  |-- cache_feats.pt (optional)
                  |-- cache_ids.pt (optional)
              |-- ntype2/
                  ...
          |-- edge_feat/
              |-- etype1/
                  |-- feats.pt
                  |-- ids.pt
                  |-- cache_feats.pt (optional)
                  |-- cache_ids.pt (optional)
              |-- etype2/
                  ...
      |-- part1/
          |-- graph/
              ...
          |-- node_feat/
              ...
          |-- edge_feat/
              ...

    """
    if 'hetero' == self.data_cls:
      node_pb_dict = {}
      for ntype in self.node_types:
        node_ids_list, node_pb = self._partition_node(ntype)
        node_feat_list = self._partition_node_feat(node_ids_list, ntype)
        for pidx in range(self.num_parts):
          if node_feat_list[pidx] is not None:
            save_feature_partition(self.output_dir, pidx, node_feat_list[pidx],
                                    group='node_feat', graph_type=ntype)
        save_node_pb(self.output_dir, node_pb, ntype)
        node_pb_dict[ntype] = node_pb

      for etype in self.edge_types:
        graph_list, edge_pb = self._partition_graph(node_pb_dict, etype)
        edge_feat_list = self._partition_edge_feat(graph_list, etype)
        for pidx in range(self.num_parts):
          save_graph_partition(self.output_dir, pidx, graph_list[pidx], etype)
          if edge_feat_list[pidx] is not None:
            save_feature_partition(self.output_dir, pidx, edge_feat_list[pidx],
                                    group='edge_feat', graph_type=etype)
        save_edge_pb(self.output_dir, edge_pb, etype)

    else:
      node_ids_list, node_pb = self._partition_node()
      node_feat_list = self._partition_node_feat(node_ids_list)
      for pidx in range(self.num_parts):
        if node_feat_list[pidx] is not None:
          save_feature_partition(self.output_dir, pidx, node_feat_list[pidx],
                                  group='node_feat')
      save_node_pb(self.output_dir, node_pb)

      graph_list, edge_pb = self._partition_graph(node_pb)
      edge_feat_list = self._partition_edge_feat(graph_list)
      for pidx in range(self.num_parts):
        save_graph_partition(self.output_dir, pidx, graph_list[pidx])
        if edge_feat_list[pidx] is not None:
          save_feature_partition(self.output_dir, pidx, edge_feat_list[pidx],
                                  group='edge_feat')
      save_edge_pb(self.output_dir, edge_pb)

    # save meta.
    save_meta(self.output_dir, self.num_parts, self.data_cls,
              self.node_types, self.edge_types)


def _load_graph_partition_data(
  graph_data_dir: str,
  device: torch.device
) -> GraphPartitionData:
  r""" Load a graph partition data from the specified directory.
  """
  if not os.path.exists(graph_data_dir):
    return None
  rows = torch.load(os.path.join(graph_data_dir, 'rows.pt'),
                    map_location=device)
  cols = torch.load(os.path.join(graph_data_dir, 'cols.pt'),
                    map_location=device)
  eids = torch.load(os.path.join(graph_data_dir, 'eids.pt'),
                    map_location=device)
  pdata = GraphPartitionData(edge_index=(rows, cols), eids=eids)
  return pdata


def _load_feature_partition_data(
  feature_data_dir: str,
  device: torch.device
) -> FeaturePartitionData:
  r""" Load a feature partition data from the specified directory.
  """
  if not os.path.exists(feature_data_dir):
    return None
  feats = torch.load(os.path.join(feature_data_dir, 'feats.pt'),
                     map_location=device)
  ids = torch.load(os.path.join(feature_data_dir, 'ids.pt'),
                   map_location=device)
  cache_feats_path = os.path.join(feature_data_dir, 'cache_feats.pt')
  cache_ids_path = os.path.join(feature_data_dir, 'cache_ids.pt')
  cache_feats = None
  cache_ids = None
  if os.path.exists(cache_feats_path) and os.path.exists(cache_ids_path):
    cache_feats = torch.load(cache_feats_path, map_location=device)
    cache_ids = torch.load(cache_ids_path, map_location=device)
  pdata = FeaturePartitionData(
    feats=feats, ids=ids, cache_feats=cache_feats, cache_ids=cache_ids
  )
  return pdata


def load_partition(
  root_dir: str,
  partition_idx: int,
  device: torch.device = torch.device('cpu')
) -> Union[Tuple[int, int,
                 GraphPartitionData,
                 Optional[FeaturePartitionData],
                 Optional[FeaturePartitionData],
                 PartitionBook,
                 PartitionBook],
           Tuple[int, int,
                 HeteroGraphPartitionData,
                 Optional[HeteroFeaturePartitionData],
                 Optional[HeteroFeaturePartitionData],
                 HeteroNodePartitionDict,
                 HeteroEdgePartitionDict]]:
  r""" Load a partition from saved directory.

  Args:
    root_dir (str): The root directory for saved files.
    partition_idx (int): The partition idx to load.
    device (torch.device): The device where loaded graph partition data locates.

  Returns:
    int: Number of all partitions.
    int: The current partition idx.
    GraphPartitionData/HeteroGraphPartitionData: graph partition data.
    FeaturePartitionData/HeteroFeaturePartitionData: node feature partition
      data, optional.
    FeaturePartitionData/HeteroFeaturePartitionData: edge feature partition
      data, optional.
    PartitionBook/HeteroNodePartitionDict: node partition book.
    PartitionBook/HeteroEdgePartitionDict: edge partition book.
  """
  with open(os.path.join(root_dir, 'META'), 'rb') as infile:
    meta = pickle.load(infile)
  num_partitions = meta['num_parts']
  assert partition_idx >= 0
  assert partition_idx < num_partitions
  partition_dir = os.path.join(root_dir, f'part{partition_idx}')
  assert os.path.exists(partition_dir)

  graph_dir = os.path.join(partition_dir, 'graph')
  node_feat_dir = os.path.join(partition_dir, 'node_feat')
  edge_feat_dir = os.path.join(partition_dir, 'edge_feat')

  # homogenous

  if meta['data_cls'] == 'homo':
    graph = _load_graph_partition_data(graph_dir, device)
    node_feat = _load_feature_partition_data(node_feat_dir, device)
    edge_feat = _load_feature_partition_data(edge_feat_dir, device)
    node_pb = torch.load(os.path.join(root_dir, 'node_pb.pt'),
                         map_location=device)
    edge_pb = torch.load(os.path.join(root_dir, 'edge_pb.pt'),
                         map_location=device)
    return (
      num_partitions, partition_idx,
      graph, node_feat, edge_feat, node_pb, edge_pb
    )

  # heterogenous

  graph_dict = {}
  for etype in meta['edge_types']:
    graph_dict[etype] = _load_graph_partition_data(
      os.path.join(graph_dir, as_str(etype)), device)

  node_feat_dict = {}
  for ntype in meta['node_types']:
    node_feat = _load_feature_partition_data(
      os.path.join(node_feat_dir, as_str(ntype)), device)
    if node_feat is not None:
      node_feat_dict[ntype] = node_feat
  if len(node_feat_dict) == 0:
    node_feat_dict = None

  edge_feat_dict = {}
  for etype in meta['edge_types']:
    edge_feat = _load_feature_partition_data(
      os.path.join(edge_feat_dir, as_str(etype)), device)
    if edge_feat is not None:
      edge_feat_dict[etype] = edge_feat
  if len(edge_feat_dict) == 0:
    edge_feat_dict = None

  node_pb_dict = {}
  node_pb_dir = os.path.join(root_dir, 'node_pb')
  for ntype in meta['node_types']:
    node_pb_dict[ntype] = torch.load(
      os.path.join(node_pb_dir, f'{as_str(ntype)}.pt'), map_location=device)

  edge_pb_dict = {}
  edge_pb_dir = os.path.join(root_dir, 'edge_pb')
  for etype in meta['edge_types']:
    edge_pb_dict[etype] = torch.load(
      os.path.join(edge_pb_dir, f'{as_str(etype)}.pt'), map_location=device)

  return (
    num_partitions, partition_idx,
    graph_dict, node_feat_dict, edge_feat_dict, node_pb_dict, edge_pb_dict
  )


def cat_feature_cache(
  partition_idx: int,
  feat_pdata: FeaturePartitionData,
  feat_pb: PartitionBook
) -> Tuple[float, torch.Tensor, torch.Tensor, PartitionBook]:
  r""" Concatenate and deduplicate partitioned features and its cached
  features into a new feature patition.

  Note that if the input `feat_pdata` does not contain a feature cache, this
  func will do nothing and return the results corresponding to the original
  partition data.

  Returns:
    float: The proportion of cache features.
    torch.Tensor: The new feature tensor, where the cached feature data is
      arranged before the original partition data.
    torch.Tensor: The tensor that indicates the mapping from global node id
      to its local index in new features.
    PartitionBook: The modified partition book for the new feature tensor.
  """
  feats = feat_pdata.feats
  ids = feat_pdata.ids
  cache_feats = feat_pdata.cache_feats
  cache_ids = feat_pdata.cache_ids
  if cache_feats is None or cache_ids is None:
    return 0.0, feats, id2idx(ids), feat_pb
  device = feats.device
  cache_ratio = cache_ids.size(0) / (cache_ids.size(0) + ids.size(0))
  # cat features
  new_feats = torch.cat([cache_feats, feats])
  # compute id2idx
  max_id = max(torch.max(cache_ids).item(), torch.max(ids).item())
  nid2idx = torch.zeros(max_id + 1, dtype=torch.int64, device=device)
  nid2idx[ids] = (torch.arange(ids.size(0), dtype=torch.int64, device=device) +
                  cache_ids.size(0))
  nid2idx[cache_ids] = torch.arange(cache_ids.size(0), dtype=torch.int64,
                                   device=device)
  # modify partition book
  new_feat_pb = feat_pb.clone()
  new_feat_pb[cache_ids] = partition_idx

  return cache_ratio, new_feats, nid2idx, new_feat_pb
