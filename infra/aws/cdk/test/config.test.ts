import {
  configFingerprint,
  EnvironmentConfig,
  openSearchServerlessResourceName,
  validateCdkEnvironment,
  validateEnvironmentConfig,
} from "../lib/config";

const VALID_CONFIG: EnvironmentConfig = {
  schemaVersion: 8,
  projectName: "local-rag-law",
  environmentName: "poc",
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
    collectionName: "local-rag-law-poc",
    indexName: "legal-rag-content-ja-v2",
    standbyReplicas: "DISABLED",
    retainOnDelete: true,
    embeddingModelId: "amazon.titan-embed-text-v2:0",
    embeddingDimensions: 1024,
    embeddingNormalize: true,
    embeddingMaxChars: 1000,
  },
  neptuneAnalytics: {
    graphName: "local-rag-law-poc",
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
    runtimeName: "LocalRagLawPoc",
    imageRepositoryName: "agent-api",
    imageTag: "agentcore-test",
    networkMode: "VPC",
  },
  tags: {
    Project: "local-rag-poc-law",
    Environment: "poc",
    ManagedBy: "aws-cdk",
  },
};

test("accepts a valid environment configuration", () => {
  expect(validateEnvironmentConfig(VALID_CONFIG, "poc")).toEqual(VALID_CONFIG);
});

test("rejects unknown fields instead of silently ignoring a typo", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      acccount: VALID_CONFIG.account,
    }),
  ).toThrow("unknown fields: acccount");
});

test("rejects a selected environment mismatch", () => {
  expect(() => validateEnvironmentConfig(VALID_CONFIG, "staging")).toThrow(
    "must match the selected environment staging",
  );
});

test("rejects duplicate repository names", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      compute: {
        ...VALID_CONFIG.compute,
        repositoryNames: ["agent-api", "agent-api"],
      },
    }),
  ).toThrow("must not contain duplicates");
});

test("rejects an invalid OpenSearch Serverless collection name", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      openSearchServerless: {
        ...VALID_CONFIG.openSearchServerless,
        collectionName: "Invalid_Name",
      },
    }),
  ).toThrow("collectionName must be a 3-32 character lowercase");
});

test("requires Titan V2 1024-dimensional embeddings for the current mapping", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      openSearchServerless: {
        ...VALID_CONFIG.openSearchServerless,
        embeddingDimensions: 512,
      },
    }),
  ).toThrow("embeddingDimensions must be 1024");
});

test("rejects an invalid embedding input limit", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      openSearchServerless: {
        ...VALID_CONFIG.openSearchServerless,
        embeddingMaxChars: 0,
      },
    }),
  ).toThrow("embeddingMaxChars must be an integer between 1 and 50000");
});

test("rejects an invalid search snapshot ID", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      bootstrapData: {
        ...VALID_CONFIG.bootstrapData,
        searchSnapshotId: "latest",
      },
    }),
  ).toThrow("searchSnapshotId is invalid");
});

test("rejects an invalid graph snapshot ID", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      bootstrapData: {
        ...VALID_CONFIG.bootstrapData,
        graphSnapshotId: "latest",
      },
    }),
  ).toThrow("graphSnapshotId is invalid");
});

test("rejects an AgentCore image repository that is not managed by compute", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      agentCore: {
        ...VALID_CONFIG.agentCore,
        imageRepositoryName: "missing-runtime",
      },
    }),
  ).toThrow("must reference compute.repositoryNames");
});

test("rejects an invalid AgentCore runtime name", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      agentCore: {
        ...VALID_CONFIG.agentCore,
        runtimeName: "local-rag-law",
      },
    }),
  ).toThrow("runtimeName is invalid");
});

test("rejects public AgentCore networking with private OpenSearch Serverless", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      agentCore: {
        ...VALID_CONFIG.agentCore,
        networkMode: "PUBLIC",
      },
    }),
  ).toThrow("must be VPC when the private OpenSearch Serverless collection");
});

test("rejects an AgentCore-unsupported Tokyo availability zone ID", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      network: {
        ...VALID_CONFIG.network,
        availabilityZoneIds: ["apne1-az1", "apne1-az3"],
      },
    }),
  ).toThrow("AZ IDs not supported by AgentCore in ap-northeast-1: apne1-az3");
});

test("rejects duplicate availability zone IDs", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      network: {
        ...VALID_CONFIG.network,
        availabilityZoneIds: ["apne1-az1", "apne1-az1"],
      },
    }),
  ).toThrow("availabilityZoneIds must not contain duplicates");
});

test("requires NAT until external service VPC endpoints are implemented", () => {
  expect(() =>
    validateEnvironmentConfig({
      ...VALID_CONFIG,
      network: {
        ...VALID_CONFIG.network,
        natGateways: 0,
      },
    }),
  ).toThrow("natGateways must be at least 1");
});

test("rejects credentials for a different AWS account", () => {
  const previousAccount = process.env.CDK_DEFAULT_ACCOUNT;
  const previousRegion = process.env.CDK_DEFAULT_REGION;
  process.env.CDK_DEFAULT_ACCOUNT = "999999999999";
  process.env.CDK_DEFAULT_REGION = VALID_CONFIG.region;
  try {
    expect(() => validateCdkEnvironment(VALID_CONFIG)).toThrow(
      "does not match configured account",
    );
  } finally {
    if (previousAccount === undefined) {
      delete process.env.CDK_DEFAULT_ACCOUNT;
    } else {
      process.env.CDK_DEFAULT_ACCOUNT = previousAccount;
    }
    if (previousRegion === undefined) {
      delete process.env.CDK_DEFAULT_REGION;
    } else {
      process.env.CDK_DEFAULT_REGION = previousRegion;
    }
  }
});

test("offline synthesis does not require AWS credentials", () => {
  expect(() => validateCdkEnvironment(VALID_CONFIG, true)).not.toThrow();
});

test("configuration fingerprint changes with an environment value", () => {
  const changed = { ...VALID_CONFIG, region: "ap-northeast-3" };
  expect(configFingerprint(changed)).not.toBe(configFingerprint(VALID_CONFIG));
});

test("bounds derived OpenSearch Serverless resource names", () => {
  const name = openSearchServerlessResourceName(
    "a-very-long-opensearch-name-1234",
    "encryption",
  );
  expect(name.length).toBeLessThanOrEqual(32);
  expect(name).toMatch(/^[a-z][a-z0-9-]*[a-z0-9]$/);
  expect(name.endsWith("-encryption")).toBe(true);
});
