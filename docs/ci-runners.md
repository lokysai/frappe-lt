# CI runner requirements

`.github/workflows/ci.yml` executes Bench and the browser on the host. Only
MariaDB and Redis are Docker services, limited to 512 MiB and 128 MiB respectively.
The two jobs are sequential. Repository-wide workflow concurrency prevents runs
on different refs from sharing ports 3306, 6379 and 8000 simultaneously.
Other repositories on the same machine must leave those ports available.

## Routing

The shared `lokysai/CI/.github/workflows/select-runner.yml@main` workflow checks
runner availability. Its primary label set is the JSON array in repository
variable `CI_RUNNER`, defaulting to `["self-hosted","linux","lokys-shop"]`.
An online runner matching all labels selects the primary, including a busy slot:
jobs queue for the self-hosted runner rather than switching to hosted because
it is busy. The fallback is GitHub-hosted `ubuntu-24.04`.

Set repository secret `CI_RUNNER_READ_TOKEN` to a token authorized to list the
organization's self-hosted runners (fine-grained token: organization self-hosted
runners, read), passed to the shared workflow as `runner-token`. Missing credentials,
API errors or no matching online runner select the hosted fallback. The lookup
does not reserve a runner: a runner becoming
unavailable after selection can still leave a job queued. Routing is chosen once
per run, before either job.

## Self-hosted Ubuntu 24.04 provisioning

The administrator must install the native prerequisites; `ghrunner` does not
need sudo. A suitable package set is:

```sh
apt-get install --yes mariadb-client libmariadb-dev pkg-config build-essential \
  git curl ca-certificates xz-utils unzip xvfb \
  libgtk-3-0t64 libgbm1 libnss3 libasound2t64 libxss1 libxtst6 \
  libatk-bridge2.0-0t64 libcups2t64 libxkbcommon0 libxcomposite1 \
  libxdamage1 libxrandr2 libdrm2
```

The runner account needs Docker access, writable runner temporary/tool-cache
directories, and network access for Actions tools, GitHub sources, Python/npm
packages and Google's Chrome download. Python 3.14 and Node 24 are supplied by
the setup actions; Bench and Yarn 1.22.22 are installed into the job's private
temporary tree.

Chrome is **not installed on the host**. On self-hosted runners,
`.github/scripts/setup-local-chrome.sh` downloads and extracts Google's stable
amd64 Debian package into the job tree and exposes a `google-chrome` wrapper on
PATH. It uses the extracted libraries plus the provisioned system libraries,
fails with the names of missing dynamic libraries. The provisioned `ci-local-chrome`
AppArmor profile grants this job-local executable permission to create its sandbox.
Cypress's own libraries and Xvfb are still required.
The Chrome build follows the current stable download, as hosted Chrome also
changes; runtime evidence records the actual browser version.

The authenticated original CSV Actions secrets remain mandatory for both jobs.
Fork PRs without these secrets cannot complete the installation gates.

## Validation boundary

Workflow contract tests check the shared selector invocation, serialization,
isolation, memory options, shell syntax and artifact semantics without building
a Bench or running browsers. Selector behavior is maintained in `lokysai/CI`.
A live Actions run is still required to verify tool-cache permissions, Docker
access, Chrome/Cypress discovery and the full pinned integration paths.

The service limit totals 640 MiB for one job, within the shared 2 GiB Docker
budget if other containers leave sufficient space. It does not prove MariaDB
and Redis peak usage or that the host's 4.5 GB slot can complete asset builds and
browser validation. Check service OOM state and host peak memory on the first
live run. Full job containers are not used.

All workflow-owned clones, Bench files, browser files and evidence live below a
unique run/attempt/job path in `runner.temp`. Evidence is uploaded before the
final `always()` cleanup, and the runtime web process is stopped first. A hard
machine/runner termination can interrupt cleanup and requires operator recovery.
GitHub concurrency retains at most one running and one pending run; a newer
pending run can replace an older pending run even with `cancel-in-progress: false`.
