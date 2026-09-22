# Fabricon 5: Deployment and Promotion

> Fabricon 5 builds on the concepts presented in [Fabricon 4](../Fabricon4/README.md).

Microsoft Fabric gives you three building blocks for CI/CD: [Git integration](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/intro-to-git-integration), [deployment pipelines](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/intro-to-deployment-pipelines), and the [REST APIs](https://learn.microsoft.com/en-us/rest/api/fabric/articles/). None of them on its own is a deployment process. Fabricon 5 composes them into one, using git as the source of truth, deployment pipelines to move content between workspaces, and a small script to orchestrate and check the result.

## The problems we are solving

Teams that edit workspaces directly and promote by hand tend to run into the same four problems.

The first is drift. Anyone can edit or create an item directly in a workspace without committing it, and the only indication is a badge on the Source control panel that nobody is watching. Given enough time, production ends up running code that exists in no branch at all.

The second is forgotten promotion. Merging a pull request updates git, not the workspace. Someone still has to open the target workspace and click Update. When that step is missed, a fix that was reviewed and merged weeks ago is still not running in production, and nothing anywhere reports it.

The third is broken references. Item IDs are assigned per workspace and are never stored in git, so anything outside Fabric that points at an item by ID breaks the moment that item is deleted and recreated. Direct Lake semantic models have a related problem: deploy one to production and it keeps reading the development lakehouse until something re-points it.

The fourth is environment leakage, where notebooks and models carry hardcoded workspace and lakehouse IDs and therefore cannot run in two environments without being edited.

All four have the same root cause. The process depends on a person remembering to do something.

## Workspace roles

Continuing the `CRM` example from [Fabricon 2](../Fabricon2/README.md), three kinds of workspace are involved, and they have different rules.

Feature workspaces are created per ticket by [branching out](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/manage-branches), which creates a branch and a workspace together in a single click. The developer owns it, works in it freely, and deletes it when the ticket is done.

`CRM-Dev` is bound to the `develop` branch and mirrors it. Developers should hold Viewer here rather than Contributor, because with feature workspaces there is no longer a reason to edit the shared workspace directly.

`CRM-Prod` has no git connection at all. It receives content only through the deployment pipeline.

> Fabricon recommends leaving the production workspace disconnected from git. If the promotion pipeline is the only way into production, direct edits are not just discouraged, they are impossible.

![Fabric - Branch out to new workspace](../Images/git-branch-to-new-workspace.png)

From the developer's side the whole workflow is: branch out, work, commit from the Source control panel, open a pull request. Everything after the merge is automated.

## The promotion pipeline

An Azure DevOps pipeline (or a GitHub Actions workflow) runs on merges to `develop` and performs seven steps. Every step raises on failure, so a partial or unverified promotion stops the run rather than continuing quietly.

1. Gate. Read the [git status](https://learn.microsoft.com/en-us/rest/api/fabric/core/git/get-status) of the Dev workspace. If any item has uncommitted changes, stop and list them. This is the step that makes drift impossible to ship.
2. Sync. [Update the workspace from git](https://learn.microsoft.com/en-us/rest/api/fabric/core/git/update-from-git). The API needs an `allowOverrideItems` consent flag to touch existing items, which is only safe because the gate has just proved there is nothing uncommitted to lose. The order of these two steps matters more than either step on its own.
3. Deploy. Call the [deployment pipeline](https://learn.microsoft.com/en-us/rest/api/fabric/core/deployment-pipelines/deploy-stage-content) to move content from the Dev stage to the Prod stage. Fabric overwrites the paired production items in place, so their IDs and URLs stay the same across every release.
4. Bind. Re-point the references that the deploy cannot fix by itself. These are covered in the next section.
5. Verify. Read every production item back and search its definition for development-stage identities: the workspace ID, item IDs, SQL endpoint names. Any match fails the release and names the item.
6. Refresh. Refresh the semantic models that were deployed. A deployment does not refresh them, and until they are refreshed the reports built on them return an error.
7. Clean up. Delete feature workspaces whose branch has been merged and removed. Two guards apply: the workspace name must match the agreed feature prefix, and the branch must actually be gone.

This folder contains a reference implementation in [`promote.py`](./promote.py), written against the standard library and configured entirely through environment variables, together with an [`azure-pipelines.yml`](./azure-pipelines.yml) template. Adding another domain is a copy of the YAML with different IDs.

> Fabricon recommends running the pipeline as a service principal through an Azure DevOps service connection rather than as a person. You will need the *Service principals can use Fabric APIs* tenant setting enabled, and the principal added as an Admin on both workspaces and on the deployment pipeline.

## Keeping references correct across stages

Most references fix themselves. Fabric maintains a pairing between each source item and its counterpart in the target stage, and during a deployment it rewires the connections between paired items: a report to its semantic model, a data pipeline to the notebook it calls, a notebook to a lakehouse in the same pipeline, and the targets of OneLake shortcuts. None of that needs configuring.

Two kinds of reference do not autobind, and the bind step exists for them:

- Notebooks whose default lakehouse lives in a different workspace, which is the normal case when data workspaces are kept separate from code workspaces. The script looks up the same-named lakehouse in the target stage and rewrites the attachment.
- Direct Lake semantic models. This is [documented behavior](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/understand-the-deployment-process#considerations-and-limitations): a deployed Direct Lake model still points at the source stage's SQL endpoint. The script matches lakehouses by name across the two stages, builds a map of their endpoints, and rewrites the connection inside the model definition.

Both rebinds work by name, which means a notebook or model that did not exist yesterday is handled correctly on its first deployment without anyone configuring anything for it.

Fabric also offers [deployment rules](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/create-rules), which can do the same re-pointing through the portal. They work, but they are set per item, only by the item's owner, they are invisible to git, and they are lost if the workspace is ever unassigned and reassigned to the pipeline.

> Fabricon recommends automating the rebinding rather than relying on deployment rules. A rule has to be created by someone who remembers that the new model needs one, and until they do, production is reading development data.

Whichever mechanism did the binding, the verify step is what makes the result trustworthy. It is cheap to run and it turns an assumption about production into a release that fails when the assumption is wrong.

### Runtime configuration

Values that change between environments belong in a [variable library](https://learn.microsoft.com/en-us/fabric/cicd/variable-library/variable-library-overview) rather than in the code. The variables and their value sets are versioned in git, while the choice of which value set is active is a per-workspace setting that survives deployments. The same committed notebook then resolves development values in Dev and production values in Prod.

Put the resolution logic in the shared `Common` notebook that every pipeline step already runs with `%run`, not in each notebook. It needs one addition for this pattern to work: a workspace that is not in the configuration is a branched-out feature workspace and should fall back to development values, with a warning rather than silently.

```python
config = variable_library_values()          # normal path
if config is None:                          # unknown workspace = branched-out feature workspace
    print("WARNING: unknown workspace - assuming feature workspace, using Dev values")
    config = dev_defaults()
```

## Working as a team

A common worry when several people are working at once is that their changes will collide during deployment. They do not, because they never meet there. Changes converge in git through pull requests, and when two people have edited the same notebook, the second pull request shows a conflict in readable text at review time. By the time `develop` moves, it is one history, and the pipeline treats a batch of forty merges exactly as it treats one.

That leaves the choice of cadence. Promoting on every merge keeps batches small, so when something does break, the change responsible is a single pull request. The alternative is a release train: let merges keep the Dev workspace current, and promote to production on a schedule or behind an [environment approval](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/approvals). The same pipeline supports either, since the sync and promote halves can be triggered separately.

> Fabricon recommends turning on *delete source branch on merge* in the repository. With the cleanup step, feature workspaces then retire themselves.

## Exceptions and limitations

Deletions do not propagate. A deployment never removes a production item that is missing from the source, so retiring something is a separate manual step that belongs in the runbook.

Some state is intentionally not copied, including schedules, permissions, credentials and the active value set. This is the behavior you want, since a development schedule should not overwrite a production one, but it does mean each stage needs configuring once. The verify step can assert the settings that matter.

Deleting an item and recreating it is the one action that breaks the guarantees here, because the replacement gets a new ID and everything referencing the old one breaks. Publish over the existing item instead. A pull request check that flags deleted `.platform` files is a reasonable guard.

Tenant-level connections and shortcuts to other workspaces may be genuinely shared between stages, or they may need to differ. Classify them when a domain is onboarded rather than assuming either.

Finally, support for some item types is still in preview for git, deployment pipelines, or both. Test a domain's inventory in a throwaway pair of workspaces before onboarding it.

## Onboarding a domain

1. Reconcile what is already in production: commit or deliberately retire every item that exists only in the workspace, resolve conflicts, and align folder structures, since pairing matches on name, type and folder.
2. Create the deployment pipeline, assign both workspaces, and confirm every item is paired before deploying anything.
3. Set the per-stage state: active value set, schedules, credentials.
4. Classify connections and shortcuts as shared or stage-specific.
5. Copy the pipeline YAML, set the IDs and the feature workspace prefix, and run it once with someone watching.
6. Move developers to Viewer on Dev, remove standing access to Prod, and enable branch policies on `develop`.

## Related links

* [CI/CD workflow options in Fabric](https://learn.microsoft.com/en-us/fabric/cicd/manage-deployment)
* [The deployment pipelines process](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/understand-the-deployment-process)
* [fabric-cicd](https://microsoft.github.io/fabric-cicd/), a Microsoft library that applies the same definition-rewriting approach used by the bind step
