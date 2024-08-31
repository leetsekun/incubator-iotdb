# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
import time

import pandas as pd
from thrift.Thrift import TException
from thrift.protocol import TCompactProtocol, TBinaryProtocol
from thrift.transport import TSocket, TTransport

from iotdb.ainode import serde
from iotdb.ainode.config import descriptor
from iotdb.ainode.constant import TSStatusCode
from iotdb.ainode.log import logger
from iotdb.ainode.util import verify_success
from iotdb.thrift.common.ttypes import TEndPoint, TSStatus, TAINodeLocation, TAINodeConfiguration
from iotdb.thrift.confignode import IConfigNodeRPCService
from iotdb.thrift.confignode.ttypes import (TAINodeRemoveReq, TNodeVersionInfo,
                                            TAINodeRegisterReq, TAINodeRestartReq)
from iotdb.thrift.datanode import IAINodeInternalRPCService
from iotdb.thrift.datanode.ttypes import (TFetchMoreDataReq,
                                          TFetchTimeseriesReq)


class ClientManager(object):
    def __init__(self):
        self.__data_node_endpoint = descriptor.get_config().get_mn_target_data_node()
        self.__config_node_endpoint = descriptor.get_config().get_ain_target_config_node_list()

    def borrow_data_node_client(self):
        return DataNodeClient(host=self.__data_node_endpoint.ip,
                              port=self.__data_node_endpoint.port)

    def borrow_config_node_client(self):
        return ConfigNodeClient(config_leader=self.__config_node_endpoint)


class DataNodeClient(object):
    DEFAULT_FETCH_SIZE = 10000
    DEFAULT_TIMEOUT = 60000

    def __init__(self, host, port):
        self.__host = host
        self.__port = port

        transport = TTransport.TFramedTransport(
            TSocket.TSocket(self.__host, self.__port)
        )
        if not transport.isOpen():
            try:
                transport.open()
            except TTransport.TTransportException as e:
                logger.error("TTransportException: {}".format(e))
                raise e

        if descriptor.get_config().get_ain_thrift_compression_enabled():
            protocol = TCompactProtocol.TCompactProtocol(transport)
        else:
            protocol = TBinaryProtocol.TBinaryProtocol(transport)
        self.__client = IAINodeInternalRPCService.Client(protocol)

    def fetch_timeseries(self,
                         query_body: str,
                         fetch_size: int = DEFAULT_FETCH_SIZE,
                         timeout: int = DEFAULT_TIMEOUT) -> pd.DataFrame:
        req = TFetchTimeseriesReq(
            queryBody=query_body,
            fetchSize=fetch_size,
            timeout=timeout
        )
        try:
            resp = self.__client.fetchTimeseries(req)
            verify_success(resp.status, "An error occurs when calling fetch_timeseries()")

            if len(resp.tsDataset) == 0:
                raise RuntimeError(f'No data fetched with sql: {query_body}')
            data = serde.convert_to_df(resp.columnNameList,
                                       resp.columnTypeList,
                                       resp.columnNameIndexMap,
                                       resp.tsDataset)
            if data.empty:
                raise RuntimeError(
                    f'Fetched empty data with sql: {query_body}')
        except Exception as e:
            logger.warning(
                f'Fail to fetch data with sql: {query_body}')
            raise e
        query_id = resp.queryId
        column_name_list = resp.columnNameList
        column_type_list = resp.columnTypeList
        column_name_index_map = resp.columnNameIndexMap
        has_more_data = resp.hasMoreData
        while has_more_data:
            req = TFetchMoreDataReq(queryId=query_id, fetchSize=fetch_size)
            try:
                resp = self.__client.fetchMoreData(req)
                verify_success(resp.status, "An error occurs when calling fetch_more_data()")
                data = data.append(serde.convert_to_df(column_name_list,
                                                       column_type_list,
                                                       column_name_index_map,
                                                       resp.tsDataset))
                has_more_data = resp.hasMoreData
            except Exception as e:
                logger.warning(
                    f'Fail to fetch more data with query id: {query_id}')
                raise e
        return data


class ConfigNodeClient(object):
    def __init__(self, config_leader: TEndPoint):
        self.__config_leader = config_leader
        self.__config_nodes = []
        self.__cursor = 0
        self.__transport = None
        self.__client = None

        self.__MSG_RECONNECTION_FAIL = "Fail to connect to any config node. Please check status of ConfigNodes"
        self.__RETRY_NUM = 5
        self.__RETRY_INTERVAL_MS = 1

        self.__try_to_connect()

    def __try_to_connect(self) -> None:
        if self.__config_leader is not None:
            try:
                self.__connect(self.__config_leader)
                return
            except TException:
                logger.warning("The current node {} may have been down, try next node", self.__config_leader)
                self.__config_leader = None

        if self.__transport is not None:
            self.__transport.close()

        try_host_num = 0
        while try_host_num < len(self.__config_nodes):
            self.__cursor = (self.__cursor + 1) % len(self.__config_nodes)

            try_endpoint = self.__config_nodes[self.__cursor]
            try:
                self.__connect(try_endpoint)
                return
            except TException:
                logger.warning("The current node {} may have been down, try next node", try_endpoint)

            try_host_num = try_host_num + 1

        raise TException(self.__MSG_RECONNECTION_FAIL)

    def __connect(self, target_config_node: TEndPoint) -> None:
        transport = TTransport.TFramedTransport(
            TSocket.TSocket(target_config_node.ip, target_config_node.port)
        )
        if not transport.isOpen():
            try:
                transport.open()
            except TTransport.TTransportException as e:
                logger.error("TTransportException: {}".format(e))
                raise e

        if descriptor.get_config().get_ain_thrift_compression_enabled():
            protocol = TCompactProtocol.TCompactProtocol(transport)
        else:
            protocol = TBinaryProtocol.TBinaryProtocol(transport)
        self.__client = IConfigNodeRPCService.Client(protocol)

    def __wait_and_reconnect(self) -> None:
        # wait to start the next try
        time.sleep(self.__RETRY_INTERVAL_MS)

        try:
            self.__try_to_connect()
        except TException:
            # can not connect to each config node
            self.__sync_latest_config_node_list()
            self.__try_to_connect()

    def __sync_latest_config_node_list(self) -> None:
        # TODO
        pass

    def __update_config_node_leader(self, status: TSStatus) -> bool:
        if status.code == TSStatusCode.REDIRECTION_RECOMMEND.get_status_code():
            if status.redirectNode is not None:
                self.__config_leader = status.redirectNode
            else:
                self.__config_leader = None
            return True
        return False

    def node_register(self, cluster_name: str, configuration: TAINodeConfiguration,
                      version_info: TNodeVersionInfo) -> int:
        req = TAINodeRegisterReq(
            clusterName=cluster_name,
            aiNodeConfiguration=configuration,
            versionInfo=version_info
        )

        for _ in range(0, self.__RETRY_NUM):
            try:
                resp = self.__client.registerAINode(req)
                if not self.__update_config_node_leader(resp.status):
                    verify_success(resp.status, "An error occurs when calling node_register()")
                    self.__config_nodes = resp.configNodeList
                    return resp.aiNodeId
            except TTransport.TException:
                logger.warning("Failed to connect to ConfigNode {} from AINode when executing node_register()",
                               self.__config_leader)
                self.__config_leader = None
            self.__wait_and_reconnect()

        raise TException(self.__MSG_RECONNECTION_FAIL)

    def node_restart(self, cluster_name: str, configuration: TAINodeConfiguration,
                     version_info: TNodeVersionInfo) -> None:
        req = TAINodeRestartReq(
            clusterName=cluster_name,
            aiNodeConfiguration=configuration,
            versionInfo=version_info
        )

        for _ in range(0, self.__RETRY_NUM):
            try:
                resp = self.__client.restartAINode(req)
                if not self.__update_config_node_leader(resp.status):
                    verify_success(resp.status, "An error occurs when calling node_restart()")
                    self.__config_nodes = resp.configNodeList
                    return resp.status
            except TTransport.TException:
                logger.warning("Failed to connect to ConfigNode {} from AINode when executing node_restart()",
                               self.__config_leader)
                self.__config_leader = None
            self.__wait_and_reconnect()

        raise TException(self.__MSG_RECONNECTION_FAIL)

    def node_remove(self, location: TAINodeLocation):
        req = TAINodeRemoveReq(
            aiNodeLocation=location
        )
        for _ in range(0, self.__RETRY_NUM):
            try:
                status = self.__client.removeAINode(req)
                if not self.__update_config_node_leader(status):
                    verify_success(status, "An error occurs when calling node_restart()")
                    return status
            except TTransport.TException:
                logger.warning("Failed to connect to ConfigNode {} from AINode when executing node_remove()",
                               self.__config_leader)
                self.__config_leader = None
            self.__wait_and_reconnect()
        raise TException(self.__MSG_RECONNECTION_FAIL)

    def get_ainode_configuration(self, node_id: int) -> map:
        for _ in range(0, self.__RETRY_NUM):
            try:
                resp = self.__client.getAINodeConfiguration(node_id)
                if not self.__update_config_node_leader(resp.status):
                    verify_success(resp.status, "An error occurs when calling get_ainode_configuration()")
                    return resp.aiNodeConfigurationMap
            except TTransport.TException:
                logger.warning("Failed to connect to ConfigNode {} from AINode when executing "
                               "get_ainode_configuration()",
                               self.__config_leader)
                self.__config_leader = None
            self.__wait_and_reconnect()
        raise TException(self.__MSG_RECONNECTION_FAIL)


client_manager = ClientManager()