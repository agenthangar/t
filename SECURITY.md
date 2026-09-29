# Security

Report vulnerabilities privately through [GitHub security advisories](https://github.com/agenthangar/t/security/advisories/new).
Do not include credentials or private agent transcripts in public issues.

`t` executes local commands, manages worktrees, and can transfer sessions over SSH.
Install only from a source you trust and review proposed changes in setup/install
wizards. Agent permission policy and automatic folder trust are opt-in; existing
agent choices are preserved. Never use an untrusted configuration file as T_LOCAL_RC:
it is executable zsh configuration, while the generated config bridge is parsed as data.

The current tagged release is the supported version. Report your version and a
minimal reproduction when filing a security report.
