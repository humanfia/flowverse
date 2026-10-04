# Security policy

Installing a flow runs its code on your machine, with your permissions, and the agents it
drives work without asking you to approve anything. Every flow in this index is reviewed before
it is listed and pinned to the commit that was reviewed, but review lowers the risk rather than
removing it: read a flow before you run it on work you care about. humanize's
[Security](https://docs.humanfia.ai/humanize/user/security) guide says what to check.

## Reporting

**Do not open a public issue.** Report privately through GitHub:
<https://github.com/humanfia/flowverse/security/advisories/new>.

Report here:

- a flow in this index that is malicious, or has been compromised: it sends data somewhere
  undisclosed, hides what it does, runs or downloads code it does not pin, harvests
  credentials, or damages what it works on;
- a published version whose tag has been moved, or whose repository has changed hands;
- a weakness in the index itself: a way past review or CI, or a way for a manifest to make hmz
  install code other than the commit it names.

Say which flow and version (`flows/<name>/<version>`), what you found and how, and anything that
shows it: a link to the code at the commit, a log, the steps. You need not have a fix.

Somewhere else:

- a bug in a flow that is not a security problem: the flow's own repository;
- a vulnerability in hmz itself:
  [humanize's security policy](https://github.com/humanfia/humanize/security/policy).

## What happens next

Maintainers aim to acknowledge a report within three working days. A flow found malicious or
compromised has every version removed from the index at once, and its name may be withheld
from reuse; hmz then stops offering it. Copies already installed stay on the machines they were
installed on, so where users need to act, a
[security advisory](https://github.com/humanfia/flowverse/security/advisories) says what to
do. Reporters are credited unless they ask not to be.
