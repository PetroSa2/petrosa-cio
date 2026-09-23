"""Regression tests for the post-petrosa_k8s#1110 GitOps deployment checkout.

petrosa_k8s#1110 moved agent tooling (including scripts/gitops-rebase-main.sh)
out of the petrosa_k8s repository and into the umbrella repository
(PetroSa2/petrosa). Every service's `deploy.yml`/`manual-deploy.yml` GitOps job
checks out a nested `petrosa_k8s` working copy and invokes
`./scripts/gitops-rebase-main.sh` from it — after #1110 that script no longer
exists there, so every GitOps push failed with
`./scripts/gitops-rebase-main.sh: No such file or directory` (exit 127),
silently stopping fixed images from ever reaching the cluster (see cio#232,
mirroring the same regression already root-caused for
petrosa-data-manager#338/#339).

These tests assert both workflow files check out the umbrella repository and
symlink its `scripts/` into the `petrosa_k8s` checkout *before* invoking the
rebase script, so this failure mode cannot silently return.
"""

from pathlib import Path

WORKFLOW_FILES = (
    Path(__file__).parents[2] / ".github" / "workflows" / "deploy.yml",
    Path(__file__).parents[2] / ".github" / "workflows" / "manual-deploy.yml",
)


def test_gitops_workflows_link_umbrella_scripts_before_rebase():
    for workflow_path in WORKFLOW_FILES:
        workflow = workflow_path.read_text()
        checkout = workflow.index("repository: PetroSa2/petrosa\n")
        rebase = workflow.index("run: ./scripts/gitops-rebase-main.sh")

        assert checkout < rebase, (
            f"{workflow_path.name}: rebase runs before umbrella checkout"
        )
        assert "path: .agent-tooling" in workflow[checkout:rebase]
        assert "clean: false" in workflow[checkout:rebase]
        assert (
            "ln -sfn ../.agent-tooling/scripts petrosa_k8s/scripts"
            in workflow[checkout:rebase]
        )
        assert (
            "test -f petrosa_k8s/scripts/gitops-rebase-main.sh"
            in workflow[checkout:rebase]
        )
