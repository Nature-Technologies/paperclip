import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

// This fork retired the release machinery by renaming it to `.disabled`
// (GitHub only reads `.yml`/`.yaml` under `.github/workflows`, so the rename
// fully deactivates a workflow while keeping it reviewable and reversible).
// The FILE CONTENT is unchanged -- both renames are R100 -- so these
// assertions still test exactly what they always tested; only the names moved.
// They are pointed at the retired names rather than deleted because `pr.yml`
// (which IS active) invokes this test file, and because the day the fork picks
// the release workflows back up, the wiring they assert must still hold.
const RELEASE_WORKFLOW = "release.yml.disabled";
const RELEASE_VERIFY_WORKFLOW = "release-verify.yml.disabled";

function readWorkflow(name) {
  return readFileSync(path.join(repoRoot, ".github/workflows", name), "utf8");
}

test("release workflow delegates stable and canary verification to the reusable workflow", () => {
  const releaseWorkflow = readWorkflow(RELEASE_WORKFLOW);

  assert.match(
    releaseWorkflow,
    /verify_canary:\n\s+if: github\.event_name == 'push'\n\s+uses: \.\/\.github\/workflows\/release-verify\.yml\n\s+with:\n\s+ref: \$\{\{ github\.sha \}\}/,
  );
  assert.match(
    releaseWorkflow,
    /verify_stable:\n\s+if: github\.event_name == 'workflow_dispatch'\n\s+uses: \.\/\.github\/workflows\/release-verify\.yml\n\s+with:\n\s+ref: \$\{\{ inputs\.source_ref \}\}/,
  );
  assert.doesNotMatch(releaseWorkflow, /verify_(?:canary|stable):[\s\S]*?pnpm test:run(?:\n|$)/);
});

test("release verify workflow covers the same split test surface as stable PR verification", () => {
  const verifyWorkflow = readWorkflow(RELEASE_VERIFY_WORKFLOW);

  assert.match(verifyWorkflow, /workflow_call:/);
  assert.match(verifyWorkflow, /node \.\/scripts\/release-package-map\.mjs check/);
  assert.match(verifyWorkflow, /pnpm -r typecheck/);
  assert.match(verifyWorkflow, /pnpm build/);

  for (const group of ["general-server", "general-workspaces-a", "general-workspaces-b"]) {
    assert.match(verifyWorkflow, new RegExp(`group: ${group}`));
  }

  for (const shardIndex of [0, 1, 2]) {
    assert.match(
      verifyWorkflow,
      new RegExp(`group: general-server[\\s\\S]*?shard_index: ${shardIndex}[\\s\\S]*?shard_count: 3`),
    );
  }

  for (const shardIndex of [0, 1, 2, 3]) {
    assert.match(verifyWorkflow, new RegExp(`shard_index: ${shardIndex}[\\s\\S]*?shard_count: 4`));
  }

  assert.match(verifyWorkflow, /pnpm test:run:general -- --group/);
  assert.match(verifyWorkflow, /pnpm test:run:serialized -- --shard-index/);
});
