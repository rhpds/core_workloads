# Field Content Workload

This workload enables self-service deployment of field-developed content using GitOps patterns. It allows developers to create their own automation in a GitOps repository and deploy it through the Red Hat Demo Platform without needing deep knowledge of AgnosticD.

## Overview

The Field Content workload either lists the community catalog as unsynced Argo Applications, or deploys one chart from a URL the orderer provided.

- **Catalog (default):** `ocp4_workload_field_content_gitops_repo_url` is empty. Creates ApplicationSet `field-content-catalog` from `catalog/tested/*.yaml` in the field-sourced-content-template repo. Generated Applications have no automated sync.
- **BYO:** URL is set. Creates Application `field-content` against that repo, same as before (automated sync with prune/selfHeal off).

Catalog tiles and the BYO parent Application stay in the Argo `default` project (the list the UI opens on). Child Applications created after Sync go to AppProject `deployed`. Charts that honor `{{ .Values.argocd.project }}` pick this up from injected Helm values.

The two modes are exclusive. The developer still owns what happens after the Application exists.

## Deployment Behavior

This role uses a **fire-and-forget** approach:

1. Creates the ArgoCD Application (BYO) or ApplicationSet (catalog)
2. Verifies the object was accepted by the cluster (~1 minute max)
3. Exits immediately — does **not** wait for sync or health status

**Why fire-and-forget?**

- **Developer ownership**: The developer is responsible for their deployment. If something fails, they debug it in ArgoCD and OpenShift — not the platform team.
- **Faster provisioning**: RHDP orders complete quickly without waiting for potentially slow deployments.
- **Reduced complexity**: No need for extensive health check configuration or retry tuning.

**What gets validated vs. what doesn't:**

| Error Type | Caught by Role? | Who Fixes It |
|------------|-----------------|--------------|
| Invalid repo URL format | ✅ Yes | Playbook fails |
| ArgoCD not installed | ✅ Yes | Playbook fails |
| Malformed Application YAML | ✅ Yes | Playbook fails |
| Repository doesn't exist | ❌ No | Developer checks ArgoCD |
| Helm chart syntax errors | ❌ No | Developer checks ArgoCD |
| Pods failing to start | ❌ No | Developer checks OpenShift |

## Hybrid Deployment Model

All deployments use Helm charts as the source type. This enables a **hybrid approach** where developers can combine:

- **Helm templates**: Standard Kubernetes manifests for operators, applications, configurations
- **Ansible Jobs**: Kubernetes Jobs running ansible-runner for complex orchestration (waiting for operators, generating secrets, calling APIs, conditional logic)

Use ArgoCD **sync waves** to control the order of deployment. For example:

- Wave 0: Namespace, RBAC setup
- Wave 1: Operator installation
- Wave 2: Ansible Job (waits for operator, performs complex setup)
- Wave 3: Application deployment

## Modes

### Community catalog (URL empty)

No GitOps URL is required. The role creates an ApplicationSet whose git files generator reads stubs from:

```yaml
ocp4_workload_field_content_catalog_repo_url: https://github.com/rhpds/field-sourced-content-template.git
ocp4_workload_field_content_catalog_repo_revision: main
ocp4_workload_field_content_catalog_files_path: catalog/tested/*.yaml
```

Each stub is a pointer (`name`, `repoURL`, `revision`, `path`). Applications are created unsynced.

### Bring your own repo (URL set)

```yaml
ocp4_workload_field_content_gitops_repo_url: "https://github.com/developer/my-field-content"
```

## Optional Variables

```yaml
ocp4_workload_field_content_gitops_repo_revision: "main"  # Git branch/tag/commit
ocp4_workload_field_content_gitops_repo_path: ""          # Path within repo (empty = root)
ocp4_workload_field_content_helm_values: {}               # Additional Helm values to merge
```

## Developer Repository Structure

Your GitOps repository must be a Helm chart. There are two recommended patterns:

### Pattern 1: App of Apps (Recommended for complex deployments)

Use this pattern when you have multiple components that should be deployed as separate ArgoCD Applications:

```
my-field-content/
├── Chart.yaml
├── values.yaml
├── templates/
│   └── applications.yaml        # Generates ArgoCD Applications for each component
└── components/
    ├── operator/
    │   ├── Chart.yaml
    │   ├── values.yaml
    │   └── templates/
    │       └── subscription.yaml
    ├── hello-world/
    │   ├── Chart.yaml
    │   ├── values.yaml
    │   └── templates/
    │       ├── namespace.yaml
    │       ├── deployment.yaml
    │       ├── service.yaml
    │       └── userinfo.yaml
    └── showroom/
        ├── Chart.yaml
        ├── values.yaml
        └── templates/
            └── showroom.yaml
```

### Pattern 2: Ansible Hybrid (For complex orchestration)

Use this pattern when you need wait-for-ready logic, secret generation, API calls, or conditional workflows:

```
my-field-content/
├── Chart.yaml
├── values.yaml
├── templates/
│   ├── namespace.yaml
│   ├── serviceaccount.yaml
│   ├── clusterrole.yaml
│   ├── clusterrolebinding.yaml
│   ├── configmap.yaml           # Ansible playbook content
│   └── job.yaml                 # ansible-runner Job
└── playbooks/
    ├── site.yml                 # Main playbook
    ├── requirements.yml         # Ansible collections
    └── roles/
        ├── operator-install/
        ├── deploy-showroom/
        └── generate-ssh-keypair/
```

## Available Deployer Values

The following values are automatically injected into your Helm chart and can be used in templates:

```yaml
deployer:
  domain: "apps.cluster-guid.guid.sandbox.opentlc.com"
  apiUrl: "https://api.cluster-guid.guid.sandbox.opentlc.com:6443"
argocd:
  project: "deployed"
```

Use these in your templates:

```yaml
# In templates/route.yaml
spec:
  host: my-app.{{ .Values.deployer.domain }}
```

## Integration with RHDP

### Labels for RHDP Integration

Use these labels to integrate with RHDP monitoring and data collection:

```yaml
metadata:
  labels:
    # Identifies this as an RHDP-managed application
    demo.redhat.com/application: "my-field-content"
```

### Passing Data Back to Users

Create ConfigMaps with the `demo.redhat.com/userinfo` label to display information in the RHDP UI:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-field-content-info
  labels:
    demo.redhat.com/userinfo: ""
data:
  demo_url: "https://my-demo.{{ .Values.deployer.domain }}"
  demo_title: "My Field Demo"
  access_instructions: "Click the demo URL to access the application"
  users_json: '{"users": {"user1": {"password": "generated-password"}}}'
```

> **Note**: While the role creates the ArgoCD Application and exits, RHDP may separately collect userinfo ConfigMaps for display in the UI.

## Monitoring Your Deployment

Since this role uses fire-and-forget, you should monitor your deployment directly:

1. **ArgoCD UI**: Check sync status and any error messages
2. **OpenShift Console**: View pod logs, events, and resource status
3. **CLI**: Use `oc get applications -n openshift-gitops` and `argocd app get field-content`

## Getting Started

1. Clone the [field-sourced-content-template](https://github.com/rhpds/field-sourced-content-template) repository
2. Choose your pattern (Helm App of Apps or Ansible Hybrid)
3. Customize the values and templates for your use case
4. Push to your own Git repository
5. Order the "Field Content" catalog item from RHDP with your repository URL
