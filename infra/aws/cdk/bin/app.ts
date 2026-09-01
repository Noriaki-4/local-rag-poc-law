#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import {
  configFingerprint,
  loadEnvironmentConfig,
  resourcePrefix,
  validateCdkEnvironment,
} from "../lib/config";
import { ComputeStack } from "../lib/compute-stack";
import { DataStack } from "../lib/data-stack";
import { ManagementStack } from "../lib/management-stack";
import { NetworkStack } from "../lib/network-stack";
import { RuntimeStack } from "../lib/runtime-stack";

const app = new cdk.App();
const selectedEnvironment = String(
  app.node.tryGetContext("environment") ??
    process.env.AWS_INFRA_ENVIRONMENT ??
    "poc",
);
const offline = String(app.node.tryGetContext("offline") ?? "false") === "true";
const config = loadEnvironmentConfig(selectedEnvironment);
validateCdkEnvironment(config, offline);

const env: cdk.Environment | undefined = offline
  ? undefined
  : { account: config.account, region: config.region };
const prefix = resourcePrefix(config);

const networkStack = new NetworkStack(app, `${prefix}-network`, {
  stackName: `${prefix}-network`,
  description: `Network foundation for ${prefix}`,
  env,
  config,
});
const dataStack = new DataStack(app, `${prefix}-data`, {
  stackName: `${prefix}-data`,
  description: `Data foundation for ${prefix}`,
  env,
  config,
  vpc: networkStack.vpc,
  terminationProtection: config.data.retainOnDelete,
});
dataStack.addStackDependency(networkStack);
const computeStack = new ComputeStack(app, `${prefix}-compute`, {
  stackName: `${prefix}-compute`,
  description: `Compute foundation for ${prefix}`,
  env,
  config,
  vpc: networkStack.vpc,
});
computeStack.addStackDependency(networkStack);

const managementStack = new ManagementStack(app, `${prefix}-management`, {
  stackName: `${prefix}-management`,
  description: `One-off management workloads for ${prefix}`,
  env,
  config,
  vpc: networkStack.vpc,
  cluster: computeStack.cluster,
  repositories: computeStack.repositories,
  logGroups: computeStack.logGroups,
  knowledgeBucket: dataStack.knowledgeBucket,
  openSearchCollection: dataStack.openSearchCollection,
  neptuneGraph: dataStack.neptuneGraph,
});
managementStack.addStackDependency(networkStack);
managementStack.addStackDependency(dataStack);
managementStack.addStackDependency(computeStack);

const stacks: cdk.Stack[] = [
  networkStack,
  dataStack,
  computeStack,
  managementStack,
];
if (config.agentCore.enabled) {
  const runtimeStack = new RuntimeStack(app, `${prefix}-runtime`, {
    stackName: `${prefix}-runtime`,
    description: `Bedrock AgentCore Runtime for ${prefix}`,
    env,
    config,
    vpc: networkStack.vpc,
    repositories: computeStack.repositories,
    knowledgeBucket: dataStack.knowledgeBucket,
    openSearchCollection: dataStack.openSearchCollection,
    neptuneGraph: dataStack.neptuneGraph,
  });
  runtimeStack.addStackDependency(networkStack);
  runtimeStack.addStackDependency(dataStack);
  runtimeStack.addStackDependency(computeStack);
  stacks.push(runtimeStack);
}

for (const stack of stacks) {
  for (const [key, value] of Object.entries(config.tags)) {
    cdk.Tags.of(stack).add(key, value);
  }
  cdk.Tags.of(stack).add(
    "ConfigurationSchemaVersion",
    String(config.schemaVersion),
  );
  cdk.Tags.of(stack).add("ConfigurationHash", configFingerprint(config));
}

app.synth();
