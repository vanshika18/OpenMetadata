#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Superset source module
"""

import traceback
from typing import Iterable, List, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.api.data.createChart import CreateChartRequest
from metadata.generated.schema.api.data.createDashboard import CreateDashboardRequest
from metadata.generated.schema.api.data.createDashboardDataModel import CreateDashboardDataModelRequest
from metadata.generated.schema.entity.data.chart import Chart, ChartType
from metadata.generated.schema.entity.data.table import Table
from metadata.generated.schema.entity.services.connections.metadata.openMetadataConnection import (
    OpenMetadataConnection,
)
from metadata.generated.schema.entity.services.databaseService import DatabaseService
from metadata.generated.schema.metadataIngestion.workflow import (
    Source as WorkflowSource,
)
from metadata.ingestion.source.dashboard.superset.mixin import SupersetSourceMixin
from metadata.ingestion.source.dashboard.superset.queries import (
    FETCH_ALL_CHARTS,
    FETCH_DASHBOARDS,
    FETCH_COLUMN
)
from metadata.generated.schema.entity.data.dashboardDataModel import (
    DashboardDataModel,
    DataModelType,
)
from metadata.generated.schema.entity.data.table import Column, DataType, Table
from metadata.utils.filters import filter_by_chart, filter_by_datamodel
from metadata.ingestion.source.database.column_type_parser import ColumnTypeParser
from metadata.utils import fqn
from metadata.utils.helpers import (
    clean_uri,
    get_database_name_for_lineage,
    get_standard_chart_type,
)
from metadata.utils.logger import ingestion_logger
from metadata.ingestion.source.dashboard.superset.models import (
    FetchDashboard, FetchChart,FetchColumn)
from sqlalchemy import sql, util

logger = ingestion_logger()


class SupersetDBSource(SupersetSourceMixin):
    """
    Superset DB Source Class
    """

    def __init__(self, config: WorkflowSource, metadata_config: OpenMetadataConnection):
        super().__init__(config, metadata_config)
        self.engine: Engine = self.client

    def prepare(self):
        """
        Fetching all charts available in superset
        this step is done because fetch_total_charts api fetches all
        the required information which is not available in fetch_charts_with_id api
        """
        charts = self.engine.execute(FETCH_ALL_CHARTS)
        for chart in charts:
            chart_detail= FetchChart(**chart)
            self.all_charts[chart_detail.id] = chart_detail

    
    def get_column_list(self, table_name) -> Optional[List[object]]:
        sql_query = sql.text(
            FETCH_COLUMN.format(
                table_name=table_name.lower()
            )
        )
        col_list = self.engine.execute(sql_query)
        return [FetchColumn(**col) for col in col_list]
   

    def get_dashboards_list(self) -> Optional[List[object]]:
        """
        Get List of all dashboards
        """
        dashboards = self.engine.execute(FETCH_DASHBOARDS)
        for dashboard in dashboards:
             yield FetchDashboard(**dashboard)
       
    def get__list(self) -> Optional[List[object]]:
        """
        Get List of all dashboards
        """
        dashboards = self.engine.execute(FETCH_DASHBOARDS)
        for dashboard in dashboards:
             yield FetchDashboard(**dashboard)

    def yield_dashboard(
        self, dashboard_details: dict
    ) -> Iterable[CreateDashboardRequest]:
        """
        Method to Get Dashboard Entity
        """
        dashboard_request = CreateDashboardRequest(
            name=dashboard_details.id,
            displayName=dashboard_details.dashboard_title,
            sourceUrl=f"{clean_uri(self.service_connection.hostPort)}/superset/dashboard/{dashboard_details.id}/",
            charts=[
                fqn.build(
                    self.metadata,
                    entity_type=Chart,
                    service_name=self.context.dashboard_service.fullyQualifiedName.__root__,
                    chart_name=chart.name.__root__,
                )
                for chart in self.context.charts
            ],
            service=self.context.dashboard_service.fullyQualifiedName.__root__,
        )
        yield dashboard_request
        self.register_record(dashboard_request=dashboard_request)

    def _get_datasource_fqn_for_lineage(self, chart_json, db_service_entity):
        return (
            self._get_datasource_fqn(chart_json, db_service_entity)
            if chart_json.table_name
            else None
        )

    def yield_dashboard_chart(
        self, dashboard_details: dict
    ) -> Optional[Iterable[CreateChartRequest]]:
        """
        Metod to fetch charts linked to dashboard
        """
        for chart_id in self._get_charts_of_dashboard(dashboard_details):
            chart_json = self.all_charts.get(chart_id)
            if not chart_json:
                logger.warning(f"chart details for id: {chart_id} not found, skipped")
                continue
            chart = CreateChartRequest(
                name=chart_json.id,
                displayName=chart_json.slice_name,
                description=chart_json.description,
                chartType=get_standard_chart_type(
                    chart_json.viz_type
                ),
                sourceUrl=f"{clean_uri(self.service_connection.hostPort)}/explore/?slice_id={chart_json.id}",
                service=self.context.dashboard_service.fullyQualifiedName.__root__,
            )
            yield chart

    def _get_database_name(
        self, sqa_str: str, db_service_entity: DatabaseService
    ) -> Optional[str]:
        default_db_name = None
        if sqa_str:
            sqa_url = make_url(sqa_str)
            default_db_name = sqa_url.database if sqa_url else None
        return get_database_name_for_lineage(db_service_entity, default_db_name)

    def _get_datasource_fqn(
        self, chart_json: dict, db_service_entity: DatabaseService
    ) -> Optional[str]:
        try:
            dataset_fqn = fqn.build(
                self.metadata,
                entity_type=Table,
                table_name=chart_json.table_name,
                database_name=self._get_database_name(
                    chart_json.sqlalchemy_uri, db_service_entity
                ),
                schema_name=chart_json.table_schema,
                service_name=db_service_entity.name.__root__,
            )
            return dataset_fqn
        except Exception as err:
            logger.debug(traceback.format_exc())
            logger.warning(
                f"Failed to fetch Datasource with id [{chart_json.table_name}]: {err}"
            )
        return None

    def yield_datamodel(
            self, dashboard_details : dict
        ) -> Iterable[CreateDashboardDataModelRequest]:
            
            if self.source_config.includeDataModels:
                for chart_id in self._get_charts_of_dashboard(dashboard_details):
                    chart_json = self.all_charts.get(chart_id)
                    if not chart_json:
                        logger.warning(f"chart details for id: {chart_id} not found, skipped")
                        continue
                    #datasource_json = self.client.fetch_datasource(chart_json.datasource_id)
                    if filter_by_datamodel(
                        self.source_config.dataModelFilterPattern, chart_json.table_name
                    ):
                        self.status.filter(chart_json.table_name, "Data model filtered out.")
                    col_names= self.get_column_list(chart_json.table_name) 
                    try:
                        data_model_request = CreateDashboardDataModelRequest(
                            name=chart_json.datasource_id,
                            displayName=chart_json.table_name,
                            service=self.context.dashboard_service.fullyQualifiedName.__root__,
                            columns=self.get_column_info(col_names),
                            dataModelType=DataModelType.SupersetDataModel.value
                        )
                        yield data_model_request
                        self.status.scanned(
                            f"Data Model Scanned: {data_model_request.displayName}"
                        )
                    except Exception as exc:
                        error_msg = f"Error yielding Data Model [{chart_json.table_name}]: {exc}"
                        self.status.failed(
                            name=chart_json.datasource_id,
                            error=error_msg,
                            stack_trace=traceback.format_exc(),
                        )
                        logger.error(error_msg)
                        logger.debug(traceback.format_exc())

    def get_column_info(self, data_source: FetchChart) -> Optional[List[Column]]:
        """
        Args:
            data_source: DataSource
        Returns:
            Columns details for Data Model
        """
        datasource_columns = []
        for field in data_source or []:
            try:
                parsed_fields = {
                    "dataTypeDisplay": field.type,
                    "dataType": ColumnTypeParser._parse_datatype_string(
                            field.type if field.type else None
                        )["dataType"],
                    "name": field.id,
                    "displayName": field.column_name,
                    "description": field.description,
                    "dataLength":  ColumnTypeParser._parse_datatype_string(
                            field.type if field.type else None
                        )["dataLength"]
                }
                # child_columns = self.get_child_columns(field=field)
                # if child_columns:
                #     parsed_fields["children"] = child_columns
                datasource_columns.append(Column(**parsed_fields))
            except Exception as exc:
                logger.debug(traceback.format_exc())
                logger.warning(f"Error to yield datamodel column: {exc}")
        return datasource_columns
    
    def yield_dashboard_lineage(
        self, dashboard_details
    ) -> Optional[Iterable[AddLineageRequest]]:
        yield from self.yield_datamodel_dashboard_lineage() or []

        for db_service_name in self.source_config.dbServiceNames or []:
            yield from self.yield_dashboard_lineage_details(
                dashboard_details, db_service_name
            ) or []


    def yield_datamodel_dashboard_lineage(
        self,
    ) -> Optional[Iterable[AddLineageRequest]]:
        """
        Returns:
            Lineage request between Data Models and Dashboards
        """
        for datamodel in self.context.dataModels or []:
            try:
                yield self._get_add_lineage_request(
                    to_entity=self.context.dashboard, from_entity=datamodel
                )
            except Exception as err:
                logger.debug(traceback.format_exc())
                logger.error(
                    f"Error to yield dashboard lineage details for data model name [{datamodel.name}]: {err}"
                )

    
        