## Scope and new resources (required)

### Stay in your repo
- Only change files in this repository. Don't edit another repo's code, workflows, deploy config, Terraform or registry references, even to unblock your own run.
- When a failure comes from another repo or service (a broken dependency, image, workflow, setting or API), open an issue in the owning repo, or comment on an existing one, with the failing run link and the error. Link it from your PR or issue, then stop work on that path and wait. Don't build a workaround, a copy, or a parallel path around it.
- Don't help another service's failing run unless an issue in that service's repo assigns the work to this repo.

### Design note before new resources
- Before adding any new Azure or cloud resource type, new service, new repository, new registry, Key Vault, database server, workspace or environment, or a second copy of anything that already exists, open a design note issue that says what it is, why the existing one can't be used, and its cost. Wait until Chris approves it in that issue.
- The PR that implements it must link the approved design note. PRs that add such resources without one will be closed.
- All cloud resources are declared in Terraform in FyberLabs/infra. Never create, change or delete cloud resources by hand (CLI, portal, SDK or scripts).
