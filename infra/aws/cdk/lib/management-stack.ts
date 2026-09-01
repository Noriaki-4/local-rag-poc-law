import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as neptunegraph from "aws-cdk-lib/aws-neptunegraph";
import * as aoss from "aws-cdk-lib/aws-opensearchserverless";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { EnvironmentConfig, openSearchServerlessResourceName } from "./config";

export interface ManagementStackProps extends cdk.StackProps {
  readonly config: EnvironmentConfig;
  readonly vpc: ec2.IVpc;
  readonly cluster: ecs.ICluster;
  readonly repositories: Readonly<Record<string, ecr.Repository>>;
  readonly logGroups: Readonly<Record<string, logs.LogGroup>>;
  readonly knowledgeBucket: s3.IBucket;
  readonly openSearchCollection: aoss.CfnCollection;
  readonly neptuneGraph: neptunegraph.CfnGraph;
}

export class ManagementStack extends cdk.Stack {
  public readonly bootstrapTask: ecs.FargateTaskDefinition;
  public readonly bootstrapSecurityGroup: ec2.SecurityGroup;

  public constructor(
    scope: Construct,
    id: string,
    props: ManagementStackProps,
  ) {
    super(scope, id, props);

    const component = props.config.bootstrapData.imageRepositoryName;
    const repository = props.repositories[component];
    const logGroup = props.logGroups[component];
    if (repository === undefined || logGroup === undefined) {
      throw new Error(
        `Bootstrap repository or log group is unavailable: ${component}`,
      );
    }

    this.bootstrapTask = new ecs.FargateTaskDefinition(this, "BootstrapTask", {
      family: `${props.config.projectName}-${props.config.environmentName}-bootstrap`,
      cpu: 1024,
      memoryLimitMiB: 4096,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });
    this.bootstrapTask.addContainer("BootstrapContainer", {
      containerName: "snapshot-bootstrap",
      image: ecs.ContainerImage.fromEcrRepository(
        repository,
        props.config.bootstrapData.imageTag,
      ),
      essential: true,
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "snapshot-bootstrap",
      }),
      environment: {
        AWS_ACCOUNT_ID: props.config.account,
        AWS_REGION: props.config.region,
        BOOTSTRAP_S3_PREFIX: props.config.bootstrapData.s3Prefix,
        SEARCH_SNAPSHOT_ID: props.config.bootstrapData.searchSnapshotId,
        GRAPH_SNAPSHOT_ID: props.config.bootstrapData.graphSnapshotId,
        CLASSIFICATION_RUN_ID: props.config.bootstrapData.classificationRunId,
        KNOWLEDGE_BUCKET_NAME: props.knowledgeBucket.bucketName,
        OPENSEARCH_URL: props.openSearchCollection.attrCollectionEndpoint,
        OPENSEARCH_INDEX: props.config.openSearchServerless.indexName,
        EMBEDDING_MODEL: props.config.openSearchServerless.embeddingModelId,
        EMBEDDING_DIMENSION: String(
          props.config.openSearchServerless.embeddingDimensions,
        ),
        EMBEDDING_MAX_CHARS: String(
          props.config.openSearchServerless.embeddingMaxChars,
        ),
        NEPTUNE_GRAPH_ID: props.neptuneGraph.attrGraphId,
      },
    });

    props.knowledgeBucket.grantRead(this.bootstrapTask.taskRole);
    props.knowledgeBucket.grantPut(this.bootstrapTask.taskRole);
    this.bootstrapTask.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "InvokeTitanEmbedding",
        actions: ["bedrock:InvokeModel"],
        resources: [
          `arn:${cdk.Aws.PARTITION}:bedrock:${props.config.region}::foundation-model/${props.config.openSearchServerless.embeddingModelId}`,
        ],
      }),
    );
    this.bootstrapTask.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "OpenSearchServerlessDataPlane",
        actions: ["aoss:APIAccessAll"],
        resources: [props.openSearchCollection.attrArn],
      }),
    );
    this.bootstrapTask.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "WriteNeptuneBootstrap",
        actions: [
          "neptune-graph:GetGraph",
          "neptune-graph:ReadDataViaQuery",
          "neptune-graph:WriteDataViaQuery",
          "neptune-graph:DeleteDataViaQuery",
        ],
        resources: [props.neptuneGraph.attrGraphArn],
      }),
    );

    const collectionName = props.config.openSearchServerless.collectionName;
    new aoss.CfnAccessPolicy(this, "OpenSearchBootstrapTaskAccessPolicy", {
      name: openSearchServerlessResourceName(collectionName, "importer"),
      type: "data",
      description: "Data access for the one-off VPC bootstrap task",
      policy: cdk.Fn.toJsonString([
        {
          Rules: [
            {
              ResourceType: "collection",
              Resource: [`collection/${collectionName}`],
              Permission: ["aoss:DescribeCollectionItems"],
            },
            {
              ResourceType: "index",
              Resource: [`index/${collectionName}/*`],
              Permission: [
                "aoss:CreateIndex",
                "aoss:DescribeIndex",
                "aoss:UpdateIndex",
                "aoss:WriteDocument",
                "aoss:ReadDocument",
              ],
            },
          ],
          Principal: [this.bootstrapTask.taskRole.roleArn],
        },
      ]),
    });

    this.bootstrapSecurityGroup = new ec2.SecurityGroup(
      this,
      "BootstrapSecurityGroup",
      {
        vpc: props.vpc,
        description: "Outbound access for the one-off legal RAG bootstrap task",
        allowAllOutbound: true,
      },
    );

    new cdk.CfnOutput(this, "BootstrapTaskDefinitionArn", {
      value: this.bootstrapTask.taskDefinitionArn,
      description: "Task definition for the explicit fixed-snapshot bootstrap",
    });
    new cdk.CfnOutput(this, "BootstrapClusterName", {
      value: props.cluster.clusterName,
      description: "ECS cluster on which to run the one-off bootstrap task",
    });
    new cdk.CfnOutput(this, "BootstrapSecurityGroupId", {
      value: this.bootstrapSecurityGroup.securityGroupId,
      description: "Security group for the one-off bootstrap task",
    });
    new cdk.CfnOutput(this, "BootstrapSubnetIds", {
      value: cdk.Fn.join(
        ",",
        props.vpc.selectSubnets({ subnetGroupName: "application" }).subnetIds,
      ),
      description: "Private subnet IDs for ecs run-task awsvpcConfiguration",
    });
  }
}
