import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { ComputeStack } from "../lib/compute-stack";
import { EnvironmentConfig } from "../lib/config";
import { DataStack } from "../lib/data-stack";
import { ManagementStack } from "../lib/management-stack";
import { NetworkStack } from "../lib/network-stack";
import { RuntimeStack } from "../lib/runtime-stack";

const CONFIG: EnvironmentConfig = {
  schemaVersion: 8,
  projectName: "local-rag-law",
  environmentName: "test",
  account: "123456789012",
  region: "ap-northeast-1",
  network: {
    cidr: "10.42.0.0/16",
    availabilityZoneIds: ["apne1-az1", "apne1-az2"],
    natGateways: 1,
  },
  data: {
    bucketName: null,
    retainOnDelete: true,
    noncurrentVersionExpirationDays: 90,
  },
  openSearchServerless: {
    collectionName: "local-rag-law-test",
    indexName: "legal-rag-content-ja-v2",
    standbyReplicas: "DISABLED",
    retainOnDelete: true,
    embeddingModelId: "amazon.titan-embed-text-v2:0",
    embeddingDimensions: 1024,
    embeddingNormalize: true,
    embeddingMaxChars: 1000,
  },
  neptuneAnalytics: {
    graphName: "local-rag-law-test",
    provisionedMemory: 16,
    replicaCount: 0,
    retainOnDelete: true,
  },
  bedrock: {
    generationModelId: "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
  },
  bootstrapData: {
    mode: "EXISTING_SNAPSHOT",
    searchSnapshotId:
      "snapshot-1e9f9f5c1ac849f7ddffdd7480f80c9f771db7c00efea06a612fc286f8c3d27e",
    graphSnapshotId:
      "snapshot-020185f383d15088b066cfbea48ff5379db05c4e1b48d69d67f209df57f0da46",
    sourceOpenSearchIndex: "legal-rag-content-ja-v2",
    sourceCorpusManifest: "datasets/lawqa_jp/egov_law_corpus/manifest.json",
    sourceGuidanceManifest: "datasets/lawqa_jp/external-guidance/manifest.json",
    scenarioManifest:
      "datasets/scenarios/public_tender_offer_three_layer_v1/manifest.json",
    classificationRunId: "classification-run-public-tender-mini-v1-v23",
    graphSchemaVersion: 9,
    s3Prefix: "knowledge-root/bootstrap/current-search-validation",
    imageRepositoryName: "bootstrap-admin",
    imageTag: "snapshot-bootstrap-v1",
  },
  compute: {
    repositoryNames: ["agent-api", "agent-ui", "bootstrap-admin"],
    lifecycleMaxImageCount: 20,
    logRetentionDays: 30,
    retainRepositoriesOnDelete: true,
  },
  agentCore: {
    enabled: true,
    runtimeName: "LocalRagLawTest",
    imageRepositoryName: "agent-api",
    imageTag: "agentcore-test",
    networkMode: "VPC",
  },
  configurationTracking: {
    dataStackHashOverride: null,
  },
  tags: {
    Project: "local-rag-poc-law",
    Environment: "test",
    ManagedBy: "aws-cdk",
  },
};

test("network stack creates public, application, and isolated data subnets", () => {
  const app = new cdk.App();
  const stack = new NetworkStack(app, "Network", { config: CONFIG });
  const template = Template.fromStack(stack);

  template.resourceCountIs("AWS::EC2::VPC", 1);
  template.resourceCountIs("AWS::EC2::Subnet", 6);
  template.resourceCountIs("AWS::EC2::NatGateway", 1);
  template.allResourcesProperties("AWS::EC2::Subnet", {
    AvailabilityZone: Match.absent(),
    AvailabilityZoneId: Match.stringLikeRegexp("^apne1-az[12]$"),
  });
  template.hasResourceProperties("AWS::EC2::VPCEndpoint", {
    VpcEndpointType: "Gateway",
    ServiceName: Match.anyValue(),
  });
});

test("data stack retains an encrypted, versioned, private knowledge bucket", () => {
  const app = new cdk.App();
  const network = new NetworkStack(app, "Network", { config: CONFIG });
  const stack = new DataStack(app, "Data", {
    config: CONFIG,
    vpc: network.vpc,
  });
  const template = Template.fromStack(stack);

  template.hasResource("AWS::S3::Bucket", {
    DeletionPolicy: "Retain",
    UpdateReplacePolicy: "Retain",
    Properties: Match.objectLike({
      BucketEncryption: Match.anyValue(),
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      VersioningConfiguration: { Status: "Enabled" },
    }),
  });
  template.hasResourceProperties("AWS::S3::BucketPolicy", {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Effect: "Deny",
          Condition: { Bool: { "aws:SecureTransport": "false" } },
        }),
      ]),
    }),
  });
  template.hasResourceProperties("AWS::OpenSearchServerless::Collection", {
    Name: "local-rag-law-test",
    Type: "VECTORSEARCH",
    StandbyReplicas: "DISABLED",
    DeletionProtection: "ENABLED",
  });
  template.resourceCountIs("AWS::OpenSearchServerless::SecurityPolicy", 2);
  template.hasResourceProperties("AWS::OpenSearchServerless::VpcEndpoint", {
    Name: "local-rag-law-test-vpce",
    VpcId: Match.anyValue(),
    SubnetIds: Match.anyValue(),
    SecurityGroupIds: Match.anyValue(),
  });
  template.hasOutput("OpenSearchCollectionEndpoint", {});
  template.hasResourceProperties("AWS::NeptuneGraph::Graph", {
    GraphName: "local-rag-law-test",
    ProvisionedMemory: 16,
    ReplicaCount: 0,
    PublicConnectivity: false,
    DeletionProtection: true,
  });
  template.hasResourceProperties("AWS::NeptuneGraph::PrivateGraphEndpoint", {
    GraphIdentifier: Match.anyValue(),
    VpcId: Match.anyValue(),
    SubnetIds: Match.anyValue(),
    SecurityGroupIds: Match.anyValue(),
  });
  template.hasResourceProperties("AWS::IAM::Role", {
    RoleName: "local-rag-law-test-bootstrap",
  });
  template.hasOutput("NeptuneGraphId", {});
  template.hasOutput("BootstrapAdminRoleArn", {});
});

test("compute stack creates a cluster, retained repositories, and bounded log groups", () => {
  const app = new cdk.App();
  const network = new NetworkStack(app, "Network", { config: CONFIG });
  const stack = new ComputeStack(app, "Compute", {
    config: CONFIG,
    vpc: network.vpc,
  });
  const template = Template.fromStack(stack);

  template.resourceCountIs("AWS::ECS::Cluster", 1);
  template.resourceCountIs("AWS::ECR::Repository", 3);
  template.resourceCountIs("AWS::Logs::LogGroup", 3);
  template.allResources("AWS::ECR::Repository", {
    DeletionPolicy: "Retain",
    UpdateReplacePolicy: "Retain",
    Properties: Match.objectLike({
      ImageScanningConfiguration: { ScanOnPush: true },
      ImageTagMutability: "IMMUTABLE",
    }),
  });
  template.allResourcesProperties("AWS::Logs::LogGroup", {
    RetentionInDays: 30,
  });
});

test("runtime stack creates a VPC AgentCore Runtime using the configured image", () => {
  const app = new cdk.App();
  const network = new NetworkStack(app, "Network", { config: CONFIG });
  const data = new DataStack(app, "Data", {
    config: CONFIG,
    vpc: network.vpc,
  });
  const compute = new ComputeStack(app, "Compute", {
    config: CONFIG,
    vpc: network.vpc,
  });
  const stack = new RuntimeStack(app, "Runtime", {
    config: CONFIG,
    vpc: network.vpc,
    repositories: compute.repositories,
    knowledgeBucket: data.knowledgeBucket,
    openSearchCollection: data.openSearchCollection,
    neptuneGraph: data.neptuneGraph,
  });
  const template = Template.fromStack(stack);

  template.resourceCountIs("AWS::BedrockAgentCore::Runtime", 1);
  template.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
    AgentRuntimeName: "LocalRagLawTest",
    NetworkConfiguration: Match.objectLike({ NetworkMode: "VPC" }),
    ProtocolConfiguration: "HTTP",
    EnvironmentVariables: Match.objectLike({
      AGENTCORE_RUNTIME: "true",
      AWS_REGION: "ap-northeast-1",
      LLM_PROVIDER: "bedrock",
      BEDROCK_MODEL_ID: "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
      LLM_MODEL: "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
      RERANK_PROVIDER: "none",
      OPENSEARCH_MODE: "serverless",
      OPENSEARCH_AWS_SERVICE: "aoss",
      OPENSEARCH_INDEX: "legal-rag-content-ja-v2",
      EMBEDDING_PROVIDER: "bedrock",
      EMBEDDING_MODEL: "amazon.titan-embed-text-v2:0",
      EMBEDDING_DIMENSION: "1024",
      EMBEDDING_NORMALIZE: "true",
      EMBEDDING_MAX_CHARS: "1000",
      GRAPH_PROVIDER: "neptune-analytics",
      NEPTUNE_GRAPH_ID: Match.anyValue(),
      NEPTUNE_GRAPH_ENDPOINT: Match.anyValue(),
      LEGAL_RELATION_CLASSIFICATION_RUN_ID:
        "classification-run-public-tender-mini-v1-v23",
      EVAL_RESULTS_DIR: "/tmp/eval-results",
    }),
  });
  template.hasResource("AWS::EC2::SecurityGroup", {
    DeletionPolicy: "Retain",
    UpdateReplacePolicy: "Retain",
  });
  template.hasOutput("AgentCoreRuntimeArn", {});
  template.hasResourceProperties("AWS::OpenSearchServerless::AccessPolicy", {
    Name: "local-rag-law-test-runtime",
    Type: "data",
  });
  template.hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: "aoss:APIAccessAll",
          Effect: "Allow",
        }),
      ]),
    }),
  });
  const policies = template.findResources("AWS::IAM::Policy");
  const renderedPolicies = JSON.stringify(policies);
  expect(renderedPolicies).toContain(
    "inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0",
  );
  expect(renderedPolicies).toContain(
    "foundation-model/amazon.titan-embed-text-v2:0",
  );
  expect(renderedPolicies).not.toContain(
    '"Action":"bedrock:InvokeModel","Effect":"Allow","Resource":"*"',
  );
});

test("management stack creates a one-off bootstrap task with a separate role", () => {
  const app = new cdk.App();
  const network = new NetworkStack(app, "NetworkForManagement", {
    config: CONFIG,
  });
  const data = new DataStack(app, "DataForManagement", {
    config: CONFIG,
    vpc: network.vpc,
  });
  const compute = new ComputeStack(app, "ComputeForManagement", {
    config: CONFIG,
    vpc: network.vpc,
  });
  const stack = new ManagementStack(app, "Management", {
    config: CONFIG,
    vpc: network.vpc,
    cluster: compute.cluster,
    repositories: compute.repositories,
    logGroups: compute.logGroups,
    knowledgeBucket: data.knowledgeBucket,
    openSearchCollection: data.openSearchCollection,
    neptuneGraph: data.neptuneGraph,
  });
  const template = Template.fromStack(stack);

  template.resourceCountIs("AWS::ECS::TaskDefinition", 1);
  template.hasResourceProperties("AWS::ECS::TaskDefinition", {
    Cpu: "1024",
    Memory: "4096",
    RequiresCompatibilities: ["FARGATE"],
    RuntimePlatform: {
      CpuArchitecture: "ARM64",
      OperatingSystemFamily: "LINUX",
    },
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: "snapshot-bootstrap",
        Environment: Match.arrayWith([
          { Name: "AWS_ACCOUNT_ID", Value: "123456789012" },
          {
            Name: "SEARCH_SNAPSHOT_ID",
            Value:
              "snapshot-1e9f9f5c1ac849f7ddffdd7480f80c9f771db7c00efea06a612fc286f8c3d27e",
          },
          { Name: "NEPTUNE_GRAPH_ID", Value: Match.anyValue() },
        ]),
      }),
    ]),
  });
  template.hasResourceProperties("AWS::OpenSearchServerless::AccessPolicy", {
    Type: "data",
    Description: "Data access for the one-off VPC bootstrap task",
  });
  template.hasOutput("BootstrapTaskDefinitionArn", {});
  template.hasOutput("BootstrapSubnetIds", {});
  const policies = template.findResources("AWS::IAM::Policy");
  expect(JSON.stringify(policies)).not.toContain("s3:DeleteObject");
});
