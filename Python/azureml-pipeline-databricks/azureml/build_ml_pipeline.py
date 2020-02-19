import argparse
import base64
import hashlib
import databricks_client
import adal
from azureml.core import Workspace, Run
from azureml.pipeline.core import Pipeline, PipelineData
from azureml.pipeline.steps import DatabricksStep
from azureml.core.compute import DatabricksCompute, ComputeTarget
from azureml.core.datastore import Datastore
from azureml.data.data_reference import DataReference
from azure.mgmt.storage import StorageManagementClient
from azureml.core.authentication import ServicePrincipalAuthentication


DATABRICKS_RUNTIME_VERSION = "6.2.x-scala2.11"


def get_databricks_compute(
    workspace: Workspace,
    compute_name: str,
):
    if compute_name not in workspace.compute_targets:
        return None
    compute_target = workspace.compute_targets[compute_name]
    if compute_target and type(compute_target) is DatabricksCompute:
        return compute_target
    raise ValueError(
        "Compute target %s is of different type" % (compute_target))


def create_databricks_compute(
    workspace: Workspace,
    compute_name: str,
    access_token: str,
):
    compute_config = DatabricksCompute.attach_configuration(
        resource_group=workspace.resource_group,
        workspace_name=args.databricks_workspace_name,
        access_token=access_token)

    compute_target = ComputeTarget.attach(
        workspace, compute_name, compute_config)

    compute_target.wait_for_completion(
        show_output=True)

    return compute_target


def upload_notebook(dbricks_client, notebook_folder,
                    notebook_dir, notebook_name):
    """
    Uploads a notebook to databricks.
    """

    # Read notebook file into a Base-64 encoded string
    with open(f"{notebook_dir}/{notebook_name}.py", "r") as file:
        file_content = file.read()
    notebook_content = file_content.encode('utf-8')
    content_b64 = base64.b64encode(notebook_content)
    notebook_checksum = hashlib.sha1(notebook_content).hexdigest()

    notebook_subfolder = f"{notebook_folder}/{notebook_checksum}"
    notebook_path = f"{notebook_subfolder}/{notebook_name}"

    # Create the notebook directory in the Databricks workspace.
    # Will not fail if the directory already exists
    dbricks_client.post(
        'workspace/mkdirs',
        json={
            "path": notebook_subfolder,
        }
    )

    # Import notebook into workspace
    dbricks_client.post(
        'workspace/import',
        json={
            "content": content_b64.decode('ascii'),
            "path": notebook_path,
            "overwrite": True,
            "language": "PYTHON",
            "format": "SOURCE"
        }
    )

    return notebook_path


def get_instance_pool(dbricks_client, pool_name):
    """
    Get the instance pool ID corresponding to an instance pool name.
    Returns None if instance pool with that name was not found.
    """
    # Query API for list of instance pools
    response = dbricks_client.get(
        'instance-pools/list',
    )
    # API quirk: 'instance_pools' element is not returned if
    # there are no instance pools.
    if 'instance_pools' in response:
        for pool in response['instance_pools']:
            if pool["instance_pool_name"] == pool_name:
                return pool["instance_pool_id"]
    return None


def main():
    """
    Builds the Azure ML pipeline for data engineering and model training.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--databricks_workspace_name",
        required=True,
        help="Name of the databricks workspace to attach"
    )
    parser.add_argument(
        "--build_id",
        default="local",
        help="Build ID for traceability"
    )
    args = parser.parse_args()


    # Get Azure machine learning workspace
    try:
        aml_run = Run.get_context(allow_offline=False)
    except:
        aml_run = None

    if aml_run:
        run_ws = aml_run.experiment.workspace
        vault = run_ws.get_default_keyvault()
        tenant_id = vault.get_secret("TenantId")
        client_id = vault.get_secret("ClientId")
        client_secret = vault.get_secret("ClientSecret")
        auth = ServicePrincipalAuthentication(
            tenant_id=tenant_id,
            service_principal_id=client_id,
            service_principal_password=client_secret)
        aml_workspace = Workspace(run_ws.subscription_id, run_ws.resource_group, run_ws.name, auth=auth)
    else:
        aml_workspace = Workspace.from_config()


    print(aml_workspace)

    # Generate Databricks credentials, see https://aka.ms/databricks-aad
    dbricks_region = aml_workspace.location
    dbricks_api = f"https://{dbricks_region}.azuredatabricks.net/api/2.0"

    vault = aml_workspace.get_default_keyvault()
    authority_host_uri = 'https://login.microsoftonline.com'
    authority_uri = authority_host_uri + '/' + vault.get_secret("TenantId")
    context = adal.AuthenticationContext(authority_uri)
    client_id = vault.get_secret("ClientId")
    client_secret = vault.get_secret("ClientSecret")

    def token_callback(resource):
        return context.acquire_token_with_client_credentials(resource, client_id, client_secret)["accessToken"]

    dbricks_client = databricks_client.create(dbricks_api)
    dbricks_client.auth_azuread(
        resource_group=aml_workspace.resource_group,
        workspace_name=args.databricks_workspace_name,
        subscription_id=aml_workspace.subscription_id,
        token_callback=token_callback)

    # Attach Databricks as Azure ML training compute
    dbricks_compute_name="databricks"
    dbricks_compute = get_databricks_compute(
        aml_workspace,
        dbricks_compute_name,
    )
    if dbricks_compute is None:
        pat_token = dbricks_client.post(
            'token/create',
            json={"comment":
                  "Azure ML Token generated by Build "
                  + args.build_id}
        )['token_value']
        dbricks_compute = create_databricks_compute(
            aml_workspace,
            dbricks_compute_name,
            pat_token,
        )

    print("dbricks_compute:")
    print(dbricks_compute)

    # Create Databricks instance pool
    pool_name = "azureml_training"
    instance_pool_id = get_instance_pool(dbricks_client,
                                         pool_name)
    if not instance_pool_id:
        dbricks_client.post(
            'instance-pools/create',
            json={
                "instance_pool_name": pool_name,
                "node_type_id": "Standard_D3_v2",
                "idle_instance_autotermination_minutes": 10,
                "preloaded_spark_versions": [DATABRICKS_RUNTIME_VERSION],
            }
        )
        instance_pool_id = get_instance_pool(dbricks_client,
                                             pool_name)

    notebook_folder = f"/Shared/AzureMLDeployed"
    workspace_datastore = Datastore(aml_workspace, "workspaceblobstore")

    # FEATURE ENGINEERING STEP (DATABRICKS)
    # Create feature engineering pipeline step

    training_datastore = Datastore(aml_workspace, "trainingdata")
    training_data_input = DataReference(
        datastore=training_datastore,
        path_on_datastore="/",
        data_reference_name="training"
    )

    feature_eng_output = PipelineData("feature_engineered",
                                      datastore=workspace_datastore)

    notebook_path = upload_notebook(
        dbricks_client, notebook_folder,
        "code/prepare", "feature_engineering")

    training_dataprep_step = DatabricksStep(
        name="FeatureEngineering",
        inputs=[training_data_input],
        outputs=[feature_eng_output],
        spark_version=DATABRICKS_RUNTIME_VERSION,
        instance_pool_id=instance_pool_id,
        num_workers=3,
        notebook_path=notebook_path,
        run_name="FeatureEngineering",
        compute_target=dbricks_compute,
        allow_reuse=True,
    )

    # You can add Azure ML model training tasks using
    #   feature_eng_output as input.
    # ...

    # Create Azure ML Pipeline
    steps = [training_dataprep_step]

    ml_pipeline = Pipeline(workspace=aml_workspace, steps=steps)
    ml_pipeline.validate()
    published_pipeline = ml_pipeline.publish(
        name="Feature Engineering",
        description="Feature engineering pipeline",
        version=args.build_id,
    )
    print(f"Published pipeline: {published_pipeline.name}")
    print(f"for build {published_pipeline.version}")


if __name__ == "__main__":
    main()
