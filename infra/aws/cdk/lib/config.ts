import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

export interface NetworkConfig {
  readonly cidr: string;
  readonly availabilityZoneIds: readonly string[];
  readonly natGateways: number;
}

export interface DataConfig {
  readonly bucketName: string | null;
  readonly retainOnDelete: boolean;
  readonly noncurrentVersionExpirationDays: number;
}

export interface OpenSearchServerlessConfig {
  readonly collectionName: string;
  readonly indexName: string;
  readonly standbyReplicas: "ENABLED" | "DISABLED";
  readonly retainOnDelete: boolean;
  readonly embeddingModelId: string;
  readonly embeddingDimensions: 1024;
  readonly embeddingNormalize: boolean;
  readonly embeddingMaxChars: number;
}

export interface NeptuneAnalyticsConfig {
  readonly graphName: string;
  readonly provisionedMemory: 16;
  readonly replicaCount: 0 | 1 | 2;
  readonly retainOnDelete: boolean;
}

export interface BedrockConfig {
  readonly generationModelId: string;
}

export interface BootstrapDataConfig {
  readonly mode: "EXISTING_SNAPSHOT";
  readonly searchSnapshotId: string;
  readonly graphSnapshotId: string;
  readonly sourceOpenSearchIndex: string;
  readonly sourceCorpusManifest: string;
  readonly sourceGuidanceManifest: string;
  readonly scenarioManifest: string;
  readonly classificationRunId: string;
  readonly graphSchemaVersion: 9;
  readonly s3Prefix: string;
  readonly imageRepositoryName: string;
  readonly imageTag: string;
}

export interface ComputeConfig {
  readonly repositoryNames: readonly string[];
  readonly lifecycleMaxImageCount: number;
  readonly logRetentionDays: number;
  readonly retainRepositoriesOnDelete: boolean;
}

export interface AgentCoreConfig {
  readonly enabled: boolean;
  readonly runtimeName: string;
  readonly imageRepositoryName: string;
  readonly imageTag: string;
  readonly networkMode: "VPC" | "PUBLIC";
}

export interface EnvironmentConfig {
  readonly schemaVersion: 8;
  readonly projectName: string;
  readonly environmentName: string;
  readonly account: string;
  readonly region: string;
  readonly network: NetworkConfig;
  readonly data: DataConfig;
  readonly openSearchServerless: OpenSearchServerlessConfig;
  readonly neptuneAnalytics: NeptuneAnalyticsConfig;
  readonly bedrock: BedrockConfig;
  readonly bootstrapData: BootstrapDataConfig;
  readonly compute: ComputeConfig;
  readonly agentCore: AgentCoreConfig;
  readonly tags: Readonly<Record<string, string>>;
}

const TOP_LEVEL_KEYS = [
  "schemaVersion",
  "projectName",
  "environmentName",
  "account",
  "region",
  "network",
  "data",
  "openSearchServerless",
  "neptuneAnalytics",
  "bedrock",
  "bootstrapData",
  "compute",
  "agentCore",
  "tags",
] as const;

const NETWORK_KEYS = ["cidr", "availabilityZoneIds", "natGateways"] as const;
const DATA_KEYS = [
  "bucketName",
  "retainOnDelete",
  "noncurrentVersionExpirationDays",
] as const;
const OPENSEARCH_SERVERLESS_KEYS = [
  "collectionName",
  "indexName",
  "standbyReplicas",
  "retainOnDelete",
  "embeddingModelId",
  "embeddingDimensions",
  "embeddingNormalize",
  "embeddingMaxChars",
] as const;
const NEPTUNE_ANALYTICS_KEYS = [
  "graphName",
  "provisionedMemory",
  "replicaCount",
  "retainOnDelete",
] as const;
const BEDROCK_KEYS = ["generationModelId"] as const;
const BOOTSTRAP_DATA_KEYS = [
  "mode",
  "searchSnapshotId",
  "graphSnapshotId",
  "sourceOpenSearchIndex",
  "sourceCorpusManifest",
  "sourceGuidanceManifest",
  "scenarioManifest",
  "classificationRunId",
  "graphSchemaVersion",
  "s3Prefix",
  "imageRepositoryName",
  "imageTag",
] as const;
const COMPUTE_KEYS = [
  "repositoryNames",
  "lifecycleMaxImageCount",
  "logRetentionDays",
  "retainRepositoriesOnDelete",
] as const;
const AGENT_CORE_KEYS = [
  "enabled",
  "runtimeName",
  "imageRepositoryName",
  "imageTag",
  "networkMode",
] as const;

const LOG_RETENTION_DAYS = new Set([
  1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653,
]);

const AGENTCORE_SUPPORTED_AZ_IDS: Readonly<
  Record<string, ReadonlySet<string>>
> = {
  "ap-northeast-1": new Set(["apne1-az1", "apne1-az2", "apne1-az4"]),
};

export function loadEnvironmentConfig(
  environmentName: string,
  configDirectory = path.resolve(process.cwd(), "../config/environments"),
): EnvironmentConfig {
  if (!/^[a-z][a-z0-9-]{0,19}$/.test(environmentName)) {
    throw new Error(`Invalid environment name: ${environmentName}`);
  }

  const configPath = path.join(configDirectory, `${environmentName}.json`);
  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(configPath, "utf8")) as unknown;
  } catch (error) {
    throw new Error(
      `Could not read environment config ${configPath}: ${errorMessage(error)}`,
    );
  }

  return validateEnvironmentConfig(parsed, environmentName, configPath);
}

export function validateEnvironmentConfig(
  value: unknown,
  expectedEnvironmentName?: string,
  source = "environment config",
): EnvironmentConfig {
  const config = requireRecord(value, source);
  requireExactKeys(config, TOP_LEVEL_KEYS, source);

  if (config.schemaVersion !== 8) {
    throw new Error(`${source}.schemaVersion must be 8`);
  }
  const projectName = requireString(
    config.projectName,
    `${source}.projectName`,
  );
  if (!/^[a-z][a-z0-9-]{2,29}$/.test(projectName)) {
    throw new Error(
      `${source}.projectName must be a lowercase resource prefix`,
    );
  }
  const environmentName = requireString(
    config.environmentName,
    `${source}.environmentName`,
  );
  if (!/^[a-z][a-z0-9-]{0,19}$/.test(environmentName)) {
    throw new Error(`${source}.environmentName is invalid`);
  }
  if (
    expectedEnvironmentName !== undefined &&
    environmentName !== expectedEnvironmentName
  ) {
    throw new Error(
      `${source}.environmentName must match the selected environment ${expectedEnvironmentName}`,
    );
  }

  const account = requireString(config.account, `${source}.account`);
  if (!/^\d{12}$/.test(account)) {
    throw new Error(`${source}.account must be a 12 digit AWS account ID`);
  }
  const region = requireString(config.region, `${source}.region`);
  if (!/^[a-z]{2}(?:-gov)?-[a-z]+-\d$/.test(region)) {
    throw new Error(`${source}.region is invalid`);
  }

  const network = validateNetwork(config.network, region, source);
  const data = validateData(config.data, source);
  const openSearchServerless = validateOpenSearchServerless(
    config.openSearchServerless,
    source,
  );
  const neptuneAnalytics = validateNeptuneAnalytics(
    config.neptuneAnalytics,
    source,
  );
  const bedrock = validateBedrock(config.bedrock, source);
  const bootstrapData = validateBootstrapData(config.bootstrapData, source);
  const compute = validateCompute(config.compute, source);
  if (!compute.repositoryNames.includes(bootstrapData.imageRepositoryName)) {
    throw new Error(
      `${source}.bootstrapData.imageRepositoryName must reference compute.repositoryNames`,
    );
  }
  const agentCore = validateAgentCore(config.agentCore, compute, source);
  const tags = validateTags(config.tags, environmentName, source);

  return {
    schemaVersion: 8,
    projectName,
    environmentName,
    account,
    region,
    network,
    data,
    openSearchServerless,
    neptuneAnalytics,
    bedrock,
    bootstrapData,
    compute,
    agentCore,
    tags,
  };
}

function validateNeptuneAnalytics(
  value: unknown,
  source: string,
): NeptuneAnalyticsConfig {
  const config = requireRecord(value, `${source}.neptuneAnalytics`);
  requireExactKeys(
    config,
    NEPTUNE_ANALYTICS_KEYS,
    `${source}.neptuneAnalytics`,
  );
  const graphName = requireString(
    config.graphName,
    `${source}.neptuneAnalytics.graphName`,
  );
  if (!/^[A-Za-z][A-Za-z0-9-]{0,61}[A-Za-z0-9]$/.test(graphName)) {
    throw new Error(`${source}.neptuneAnalytics.graphName is invalid`);
  }
  if (config.provisionedMemory !== 16) {
    throw new Error(
      `${source}.neptuneAnalytics.provisionedMemory must be 16 for the initial PoC`,
    );
  }
  if (![0, 1, 2].includes(Number(config.replicaCount))) {
    throw new Error(
      `${source}.neptuneAnalytics.replicaCount must be 0, 1, or 2`,
    );
  }
  const retainOnDelete = requireBoolean(
    config.retainOnDelete,
    `${source}.neptuneAnalytics.retainOnDelete`,
  );
  return {
    graphName,
    provisionedMemory: 16,
    replicaCount: config.replicaCount as 0 | 1 | 2,
    retainOnDelete,
  };
}

function validateBedrock(value: unknown, source: string): BedrockConfig {
  const config = requireRecord(value, `${source}.bedrock`);
  requireExactKeys(config, BEDROCK_KEYS, `${source}.bedrock`);
  const generationModelId = requireString(
    config.generationModelId,
    `${source}.bedrock.generationModelId`,
  );
  if (generationModelId !== "jp.anthropic.claude-haiku-4-5-20251001-v1:0") {
    throw new Error(
      `${source}.bedrock.generationModelId must be the Japan Claude Haiku 4.5 inference profile`,
    );
  }
  return { generationModelId };
}

function validateBootstrapData(
  value: unknown,
  source: string,
): BootstrapDataConfig {
  const config = requireRecord(value, `${source}.bootstrapData`);
  requireExactKeys(config, BOOTSTRAP_DATA_KEYS, `${source}.bootstrapData`);
  if (config.mode !== "EXISTING_SNAPSHOT") {
    throw new Error(`${source}.bootstrapData.mode must be EXISTING_SNAPSHOT`);
  }
  const searchSnapshotId = requireSnapshotId(
    config.searchSnapshotId,
    `${source}.bootstrapData.searchSnapshotId`,
  );
  const graphSnapshotId = requireSnapshotId(
    config.graphSnapshotId,
    `${source}.bootstrapData.graphSnapshotId`,
  );
  const sourceOpenSearchIndex = requireString(
    config.sourceOpenSearchIndex,
    `${source}.bootstrapData.sourceOpenSearchIndex`,
  );
  if (!/^[a-z0-9][a-z0-9._-]{0,254}$/.test(sourceOpenSearchIndex)) {
    throw new Error(`${source}.bootstrapData.sourceOpenSearchIndex is invalid`);
  }
  const sourceCorpusManifest = requireRepositoryRelativePath(
    config.sourceCorpusManifest,
    `${source}.bootstrapData.sourceCorpusManifest`,
  );
  const sourceGuidanceManifest = requireRepositoryRelativePath(
    config.sourceGuidanceManifest,
    `${source}.bootstrapData.sourceGuidanceManifest`,
  );
  const scenarioManifest = requireRepositoryRelativePath(
    config.scenarioManifest,
    `${source}.bootstrapData.scenarioManifest`,
  );
  const classificationRunId = requireString(
    config.classificationRunId,
    `${source}.bootstrapData.classificationRunId`,
  );
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$/.test(classificationRunId)) {
    throw new Error(`${source}.bootstrapData.classificationRunId is invalid`);
  }
  if (config.graphSchemaVersion !== 9) {
    throw new Error(`${source}.bootstrapData.graphSchemaVersion must be 9`);
  }
  const s3Prefix = requireString(
    config.s3Prefix,
    `${source}.bootstrapData.s3Prefix`,
  );
  if (
    s3Prefix.startsWith("/") ||
    s3Prefix.endsWith("/") ||
    s3Prefix.includes("..") ||
    !/^[A-Za-z0-9!_.*'()/-]+$/.test(s3Prefix)
  ) {
    throw new Error(`${source}.bootstrapData.s3Prefix is invalid`);
  }
  const imageRepositoryName = requireString(
    config.imageRepositoryName,
    `${source}.bootstrapData.imageRepositoryName`,
  );
  const imageTag = requireString(
    config.imageTag,
    `${source}.bootstrapData.imageTag`,
  );
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(imageTag)) {
    throw new Error(`${source}.bootstrapData.imageTag is invalid`);
  }
  return {
    mode: "EXISTING_SNAPSHOT",
    searchSnapshotId,
    graphSnapshotId,
    sourceOpenSearchIndex,
    sourceCorpusManifest,
    sourceGuidanceManifest,
    scenarioManifest,
    classificationRunId,
    graphSchemaVersion: 9,
    s3Prefix,
    imageRepositoryName,
    imageTag,
  };
}

function requireSnapshotId(value: unknown, field: string): string {
  const snapshotId = requireString(value, field);
  if (!/^snapshot-[a-f0-9]{64}$/.test(snapshotId)) {
    throw new Error(`${field} is invalid`);
  }
  return snapshotId;
}

function requireRepositoryRelativePath(value: unknown, field: string): string {
  const relativePath = requireString(value, field);
  if (
    relativePath.startsWith("/") ||
    relativePath.includes("..") ||
    !/^[A-Za-z0-9._/-]+\.json$/.test(relativePath)
  ) {
    throw new Error(`${field} must be a repository-relative JSON path`);
  }
  return relativePath;
}

function validateOpenSearchServerless(
  value: unknown,
  source: string,
): OpenSearchServerlessConfig {
  const config = requireRecord(value, `${source}.openSearchServerless`);
  requireExactKeys(
    config,
    OPENSEARCH_SERVERLESS_KEYS,
    `${source}.openSearchServerless`,
  );
  const collectionName = requireString(
    config.collectionName,
    `${source}.openSearchServerless.collectionName`,
  );
  if (!/^[a-z][a-z0-9-]{1,30}[a-z0-9]$/.test(collectionName)) {
    throw new Error(
      `${source}.openSearchServerless.collectionName must be a 3-32 character lowercase OpenSearch Serverless name`,
    );
  }
  const indexName = requireString(
    config.indexName,
    `${source}.openSearchServerless.indexName`,
  );
  if (!/^[a-z0-9][a-z0-9._-]{0,254}$/.test(indexName)) {
    throw new Error(`${source}.openSearchServerless.indexName is invalid`);
  }
  if (
    config.standbyReplicas !== "ENABLED" &&
    config.standbyReplicas !== "DISABLED"
  ) {
    throw new Error(
      `${source}.openSearchServerless.standbyReplicas must be ENABLED or DISABLED`,
    );
  }
  const retainOnDelete = requireBoolean(
    config.retainOnDelete,
    `${source}.openSearchServerless.retainOnDelete`,
  );
  const embeddingModelId = requireString(
    config.embeddingModelId,
    `${source}.openSearchServerless.embeddingModelId`,
  );
  if (embeddingModelId !== "amazon.titan-embed-text-v2:0") {
    throw new Error(
      `${source}.openSearchServerless.embeddingModelId must be amazon.titan-embed-text-v2:0`,
    );
  }
  if (config.embeddingDimensions !== 1024) {
    throw new Error(
      `${source}.openSearchServerless.embeddingDimensions must be 1024`,
    );
  }
  const embeddingNormalize = requireBoolean(
    config.embeddingNormalize,
    `${source}.openSearchServerless.embeddingNormalize`,
  );
  const embeddingMaxChars = requireInteger(
    config.embeddingMaxChars,
    `${source}.openSearchServerless.embeddingMaxChars`,
    1,
    50_000,
  );
  return {
    collectionName,
    indexName,
    standbyReplicas: config.standbyReplicas,
    retainOnDelete,
    embeddingModelId,
    embeddingDimensions: 1024,
    embeddingNormalize,
    embeddingMaxChars,
  };
}

export function validateCdkEnvironment(
  config: EnvironmentConfig,
  offline = false,
): void {
  if (offline) {
    return;
  }
  const cliAccount = process.env.CDK_DEFAULT_ACCOUNT;
  const cliRegion = process.env.CDK_DEFAULT_REGION;
  if (cliAccount === undefined || cliRegion === undefined) {
    throw new Error(
      "CDK could not resolve the credential account and region; select an AWS profile or use the offline synth command",
    );
  }
  if (cliAccount !== config.account) {
    throw new Error(
      `CDK credential account ${cliAccount} does not match configured account ${config.account}`,
    );
  }
  if (cliRegion !== config.region) {
    throw new Error(
      `CDK credential region ${cliRegion} does not match configured region ${config.region}`,
    );
  }
}

export function resourcePrefix(config: EnvironmentConfig): string {
  return `${config.projectName}-${config.environmentName}`;
}

export function openSearchServerlessResourceName(
  collectionName: string,
  suffix: string,
): string {
  const maximumBaseLength = 32 - suffix.length - 1;
  if (maximumBaseLength < 3 || !/^[a-z][a-z0-9-]*[a-z0-9]$/.test(suffix)) {
    throw new Error(`Invalid OpenSearch Serverless resource suffix: ${suffix}`);
  }
  const base = collectionName.slice(0, maximumBaseLength).replace(/-+$/, "");
  return `${base}-${suffix}`;
}

export function configFingerprint(config: EnvironmentConfig): string {
  return createHash("sha256")
    .update(JSON.stringify(sortForHash(config)))
    .digest("hex")
    .slice(0, 16);
}

function validateNetwork(
  value: unknown,
  region: string,
  source: string,
): NetworkConfig {
  const network = requireRecord(value, `${source}.network`);
  requireExactKeys(network, NETWORK_KEYS, `${source}.network`);
  const cidr = requireString(network.cidr, `${source}.network.cidr`);
  const cidrMatch = /^(\d{1,3}(?:\.\d{1,3}){3})\/(\d{1,2})$/.exec(cidr);
  if (
    cidrMatch === null ||
    Number(cidrMatch[2]) < 16 ||
    Number(cidrMatch[2]) > 24
  ) {
    throw new Error(
      `${source}.network.cidr must be an IPv4 /16 through /24 CIDR`,
    );
  }
  for (const octet of cidrMatch[1].split(".")) {
    if (Number(octet) > 255) {
      throw new Error(`${source}.network.cidr contains an invalid IPv4 octet`);
    }
  }
  if (
    !Array.isArray(network.availabilityZoneIds) ||
    network.availabilityZoneIds.length < 2 ||
    network.availabilityZoneIds.length > 3
  ) {
    throw new Error(
      `${source}.network.availabilityZoneIds must contain 2 or 3 AZ IDs`,
    );
  }
  const availabilityZoneIds = network.availabilityZoneIds.map(
    (rawZoneId, index) => {
      const zoneId = requireString(
        rawZoneId,
        `${source}.network.availabilityZoneIds[${index}]`,
      );
      if (!/^[a-z0-9]+-az[1-9][0-9]*$/.test(zoneId)) {
        throw new Error(
          `${source}.network.availabilityZoneIds[${index}] is invalid`,
        );
      }
      return zoneId;
    },
  );
  if (new Set(availabilityZoneIds).size !== availabilityZoneIds.length) {
    throw new Error(
      `${source}.network.availabilityZoneIds must not contain duplicates`,
    );
  }
  const supportedZoneIds = AGENTCORE_SUPPORTED_AZ_IDS[region];
  const unsupportedZoneIds =
    supportedZoneIds === undefined
      ? []
      : availabilityZoneIds.filter((zoneId) => !supportedZoneIds.has(zoneId));
  if (unsupportedZoneIds.length > 0) {
    throw new Error(
      `${source}.network.availabilityZoneIds contains AZ IDs not supported by AgentCore in ${region}: ${unsupportedZoneIds.join(", ")}`,
    );
  }
  const natGateways = requireInteger(
    network.natGateways,
    `${source}.network.natGateways`,
    0,
    availabilityZoneIds.length,
  );
  if (natGateways === 0) {
    throw new Error(
      `${source}.network.natGateways must be at least 1 until ECR, Logs, and Bedrock VPC endpoints are implemented`,
    );
  }
  return { cidr, availabilityZoneIds, natGateways };
}

function validateData(value: unknown, source: string): DataConfig {
  const data = requireRecord(value, `${source}.data`);
  requireExactKeys(data, DATA_KEYS, `${source}.data`);
  let bucketName: string | null = null;
  if (data.bucketName !== null) {
    bucketName = requireString(data.bucketName, `${source}.data.bucketName`);
    if (!/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(bucketName)) {
      throw new Error(
        `${source}.data.bucketName is not a valid S3 bucket name`,
      );
    }
  }
  const retainOnDelete = requireBoolean(
    data.retainOnDelete,
    `${source}.data.retainOnDelete`,
  );
  const noncurrentVersionExpirationDays = requireInteger(
    data.noncurrentVersionExpirationDays,
    `${source}.data.noncurrentVersionExpirationDays`,
    1,
    3650,
  );
  return { bucketName, retainOnDelete, noncurrentVersionExpirationDays };
}

function validateCompute(value: unknown, source: string): ComputeConfig {
  const compute = requireRecord(value, `${source}.compute`);
  requireExactKeys(compute, COMPUTE_KEYS, `${source}.compute`);
  if (
    !Array.isArray(compute.repositoryNames) ||
    compute.repositoryNames.length === 0
  ) {
    throw new Error(
      `${source}.compute.repositoryNames must be a non-empty array`,
    );
  }
  const repositoryNames = compute.repositoryNames.map((item, index) => {
    const name = requireString(
      item,
      `${source}.compute.repositoryNames[${index}]`,
    );
    if (!/^[a-z][a-z0-9-]{1,62}$/.test(name)) {
      throw new Error(`${source}.compute.repositoryNames[${index}] is invalid`);
    }
    return name;
  });
  if (new Set(repositoryNames).size !== repositoryNames.length) {
    throw new Error(
      `${source}.compute.repositoryNames must not contain duplicates`,
    );
  }
  const lifecycleMaxImageCount = requireInteger(
    compute.lifecycleMaxImageCount,
    `${source}.compute.lifecycleMaxImageCount`,
    1,
    1000,
  );
  const logRetentionDays = requireInteger(
    compute.logRetentionDays,
    `${source}.compute.logRetentionDays`,
    1,
    3653,
  );
  if (!LOG_RETENTION_DAYS.has(logRetentionDays)) {
    throw new Error(
      `${source}.compute.logRetentionDays is not supported by CloudWatch Logs`,
    );
  }
  const retainRepositoriesOnDelete = requireBoolean(
    compute.retainRepositoriesOnDelete,
    `${source}.compute.retainRepositoriesOnDelete`,
  );
  return {
    repositoryNames,
    lifecycleMaxImageCount,
    logRetentionDays,
    retainRepositoriesOnDelete,
  };
}

function validateAgentCore(
  value: unknown,
  compute: ComputeConfig,
  source: string,
): AgentCoreConfig {
  const agentCore = requireRecord(value, `${source}.agentCore`);
  requireExactKeys(agentCore, AGENT_CORE_KEYS, `${source}.agentCore`);
  const enabled = requireBoolean(
    agentCore.enabled,
    `${source}.agentCore.enabled`,
  );
  const runtimeName = requireString(
    agentCore.runtimeName,
    `${source}.agentCore.runtimeName`,
  );
  if (!/^[A-Za-z][A-Za-z0-9_]{0,47}$/.test(runtimeName)) {
    throw new Error(`${source}.agentCore.runtimeName is invalid`);
  }
  const imageRepositoryName = requireString(
    agentCore.imageRepositoryName,
    `${source}.agentCore.imageRepositoryName`,
  );
  if (!compute.repositoryNames.includes(imageRepositoryName)) {
    throw new Error(
      `${source}.agentCore.imageRepositoryName must reference compute.repositoryNames`,
    );
  }
  const imageTag = requireString(
    agentCore.imageTag,
    `${source}.agentCore.imageTag`,
  );
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(imageTag)) {
    throw new Error(`${source}.agentCore.imageTag is invalid`);
  }
  if (agentCore.networkMode !== "VPC" && agentCore.networkMode !== "PUBLIC") {
    throw new Error(`${source}.agentCore.networkMode must be VPC or PUBLIC`);
  }
  if (enabled && agentCore.networkMode !== "VPC") {
    throw new Error(
      `${source}.agentCore.networkMode must be VPC when the private OpenSearch Serverless collection is enabled`,
    );
  }
  return {
    enabled,
    runtimeName,
    imageRepositoryName,
    imageTag,
    networkMode: agentCore.networkMode,
  };
}

function validateTags(
  value: unknown,
  environmentName: string,
  source: string,
): Readonly<Record<string, string>> {
  const tags = requireRecord(value, `${source}.tags`);
  if (Object.keys(tags).length === 0) {
    throw new Error(`${source}.tags must not be empty`);
  }
  const validated: Record<string, string> = {};
  for (const [key, rawValue] of Object.entries(tags)) {
    if (
      key.length === 0 ||
      key.length > 128 ||
      key.toLowerCase().startsWith("aws:")
    ) {
      throw new Error(`${source}.tags contains an invalid key: ${key}`);
    }
    validated[key] = requireString(rawValue, `${source}.tags.${key}`);
  }
  if (validated.Environment !== environmentName) {
    throw new Error(`${source}.tags.Environment must match environmentName`);
  }
  if (validated.ManagedBy !== "aws-cdk") {
    throw new Error(`${source}.tags.ManagedBy must be aws-cdk`);
  }
  return validated;
}

function requireRecord(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function requireExactKeys(
  value: Record<string, unknown>,
  allowedKeys: readonly string[],
  name: string,
): void {
  const allowed = new Set(allowedKeys);
  const unexpected = Object.keys(value).filter((key) => !allowed.has(key));
  const missing = allowedKeys.filter((key) => !(key in value));
  if (unexpected.length > 0) {
    throw new Error(
      `${name} contains unknown fields: ${unexpected.join(", ")}`,
    );
  }
  if (missing.length > 0) {
    throw new Error(`${name} is missing fields: ${missing.join(", ")}`);
  }
}

function requireString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value;
}

function requireBoolean(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`${name} must be a boolean`);
  }
  return value;
}

function requireInteger(
  value: unknown,
  name: string,
  minimum: number,
  maximum: number,
): number {
  if (
    !Number.isInteger(value) ||
    Number(value) < minimum ||
    Number(value) > maximum
  ) {
    throw new Error(
      `${name} must be an integer between ${minimum} and ${maximum}`,
    );
  }
  return Number(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function sortForHash(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortForHash);
  }
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, sortForHash(item)]),
    );
  }
  return value;
}
