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
Source connection handler
"""
from functools import partial

# from functools import partial
from typing import Optional

from pydantic import BaseModel
from couchbase.cluster import Cluster
from couchbase.options import ClusterOptions
from couchbase.auth import PasswordAuthenticator
from couchbase.exceptions import CouchbaseException
from couchbase.management.collections import CollectionManager

from metadata.generated.schema.entity.automations.workflow import (
    Workflow as AutomationWorkflow,
)
from metadata.generated.schema.entity.services.connections.database.couchbaseConnection import (
    CouchbaseConnection,
)
from metadata.ingestion.connections.builders import get_connection_url_common
from metadata.ingestion.connections.test_connections import test_connection_steps
from metadata.ingestion.ometa.ometa_api import OpenMetadata


def get_connection(connection: CouchbaseConnection):
    """
    Create connection
    """
    try:
        couchbase_cluster = Cluster.connect(connection.endpoint, ClusterOptions(PasswordAuthenticator(connection.username, "Vanshu@123")))
        return couchbase_cluster
    except CouchbaseException as e:    
        print(e)
    return ""
          
    

def test_connection(
    metadata: OpenMetadata,
    client: Cluster,
    service_connection: CouchbaseConnection,
    automation_workflow: Optional[AutomationWorkflow] = None,
) -> None:
    """
    Test connection. This can be executed either as part
    of a metadata workflow or during an Automation Workflow
    """

    class SchemaHolder(BaseModel):
        database: Optional[str]

    holder = SchemaHolder()

    def test_get_databases(client: Cluster, holder: SchemaHolder):
        print("client is ",client)
        buckets = client.buckets()
        list_bucket=buckets.get_all_buckets()
        for database in list_bucket:
            holder.database = database.name
            break

    def test_get_collections(client: Cluster, holder: SchemaHolder):
        database =  client.bucket(holder.database)
        collection_manager = database.collections()
        collection_manager.get_all_scopes()
            
    test_fn = {
        "CheckAccess": lambda: True,
        "GetDatabases": partial(test_get_databases, client, holder),
        "GetCollections": partial(test_get_collections, client, holder),
    }

    test_connection_steps(
        metadata=metadata,
        test_fn=test_fn,
        service_type=service_connection.type.value,
        automation_workflow=automation_workflow,
    )
    

