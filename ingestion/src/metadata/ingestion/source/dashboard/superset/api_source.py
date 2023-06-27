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

from metadata.generated.schema.api.data.createChart import CreateChartRequest
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.api.data.createDashboard import CreateDashboardRequest
from metadata.generated.schema.api.data.createDashboardDataModel import CreateDashboardDataModelRequest
from metadata.generated.schema.entity.data.chart import Chart, ChartType
from metadata.generated.schema.entity.data.table import Table
from metadata.generated.schema.entity.services.databaseService import DatabaseService
from metadata.ingestion.source.dashboard.superset.mixin import SupersetSourceMixin
from metadata.generated.schema.entity.data.dashboardDataModel import (
    DashboardDataModel,
    DataModelType,
)
from metadata.utils.filters import filter_by_chart, filter_by_datamodel
from metadata.utils import fqn
from metadata.utils.helpers import (
    clean_uri,
    get_database_name_for_lineage,
    get_standard_chart_type,
)
from metadata.utils.logger import ingestion_logger
from metadata.ingestion.source.dashboard.superset.models import (SupersetDatasource,
                                                                 DataSourceResult)
from metadata.ingestion.source.database.column_type_parser import ColumnTypeParser
from metadata.generated.schema.entity.data.table import Column, DataType, Table



logger = ingestion_logger()


class SupersetAPISource(SupersetSourceMixin):
    """
    Superset API Source Class
    """

    def prepare(self):
        """
        Fetching all charts available in superset
        this step is done because fetch_total_charts api fetches all
        the required information which is not available in fetch_charts_with_id api
        """
        current_page = 0
        page_size = 25
        total_charts = self.client.fetch_total_charts()
        while current_page * page_size <= total_charts:
            charts = self.client.fetch_charts(current_page, page_size)
            current_page += 1
            # SupersetChart.result
            for index in range(len(charts.result)):
                self.all_charts[charts.ids[index]] = charts.result[index]

    def get_dashboards_list(self) -> Optional[List[object]]:
        """
        Get List of all dashboards
        """
        current_page = 0
        page_size = 25
        total_dashboards = self.client.fetch_total_dashboards()
        while current_page * page_size <= total_dashboards:
            dashboards = self.client.fetch_dashboards(current_page, page_size)
            current_page += 1
            for dashboard in dashboards.result:
                yield dashboard
    
    def yield_dashboard(
        self, dashboard_details: dict
    ) -> Iterable[CreateDashboardRequest]:
        """
        Method to Get Dashboard Entity
        """
        dashboard_request = CreateDashboardRequest(
            name=dashboard_details.id,
            displayName=dashboard_details.dashboard_title,
            sourceUrl=f"{clean_uri(self.service_connection.hostPort)}{dashboard_details.url}",
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
            self._get_datasource_fqn(chart_json.datasource_id, db_service_entity)
            if chart_json.datasource_id
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
                    # chart_json.get("viz_type", ChartType.Other.value)
                    chart_json.viz_type
                ),
                sourceUrl=f"{clean_uri(self.service_connection.hostPort)}{chart_json.url}",
                service=self.context.dashboard_service.fullyQualifiedName.__root__,
            )
            yield chart

    def _get_datasource_fqn(
        self, datasource_id: str, db_service_entity: DatabaseService
    ) -> Optional[str]:
        try:
            datasource_json = self.client.fetch_datasource(datasource_id)
            if datasource_json:
                database_json = self.client.fetch_database(
                    datasource_json.result.database.id
                )
                default_database_name = (
                    database_json.result.parameters.database
                    if database_json.result.parameters
                    else None
                )

                database_name = get_database_name_for_lineage(
                    db_service_entity, default_database_name
                )

                if database_json:
                    dataset_fqn = fqn.build(
                        self.metadata,
                        entity_type=Table,
                        table_name=datasource_json.result.table_name,
                        schema_name=datasource_json.result.table_schema,
                        database_name=database_name,
                        service_name=db_service_entity.name.__root__,
                    )
                return dataset_fqn
        except KeyError as err:
            logger.debug(traceback.format_exc())
            logger.warning(
                f"Failed to fetch Datasource with id [{datasource_id}]: {err}"
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
                datasource_json = self.client.fetch_datasource(chart_json.datasource_id)
                if filter_by_datamodel(
                    self.source_config.dataModelFilterPattern, datasource_json.result.table_name
                ):
                    self.status.filter(datasource_json.result.table_name, "Data model filtered out.")
                    
                try:
                    data_model_request = CreateDashboardDataModelRequest(
                        name=datasource_json.id,
                        displayName=datasource_json.result.table_name,
                        service=self.context.dashboard_service.fullyQualifiedName.__root__,
                        columns=self.get_column_info(datasource_json.result.columns),
                        dataModelType=DataModelType.SupersetDataModel.value
                    )
                    yield data_model_request
                    self.status.scanned(
                        f"Data Model Scanned: {data_model_request.displayName}"
                    )
                except Exception as exc:
                    error_msg = f"Error yielding Data Model [{datasource_json.result.table_name}]: {exc}"
                    self.status.failed(
                        name=datasource_json.id,
                        error=error_msg,
                        stack_trace=traceback.format_exc(),
                    )
                    logger.error(error_msg)
                    logger.debug(traceback.format_exc())

    def get_column_info(self, data_source: DataSourceResult) -> Optional[List[Column]]:
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
                    # "dataType": DataType.field.data,
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

    
        
        