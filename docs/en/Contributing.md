# Contributing Guide

Thank you for your interest in JiuwenSwarm! Whether it's filing bugs, developing features, improving documentation, or sharing Skills — every contribution is valuable.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How to Contribute](#how-to-contribute)
  - [Filing Issues](#filing-issues)
  - [Submitting Pull Requests](#submitting-pull-requests)
  - [Sharing Skills](#sharing-skills)
- [Development Environment Setup](#development-environment-setup)
- [Code Standards](#code-standards)
  - [Code Style](#code-style)
  - [Commit Message Convention](#commit-message-convention)
  - [Branch Naming Convention](#branch-naming-convention)
- [PR Review Process](#pr-review-process)
- [Version Releases](#version-releases)

---

## Code of Conduct

- Respect every contributor; communicate kindly
- Keep Issues and PRs objective and focused on the problem
- Welcome developers of all skill levels; be helpful to newcomers

---

## How to Contribute

### Filing Issues

Submit on [GitCode Issues](https://gitcode.com/openJiuwen/jiuwenswarm/issues) with the appropriate type:

| Type | Template | Description |
|------|----------|-------------|
| Bug Report | Bug Report | Include reproduction steps, expected behavior, actual behavior, environment info |
| Feature Request | Feature Request | Describe the scenario, expected outcome, and alternatives |
| Question | Question | Describe the problem and troubleshooting steps already tried |

**Bug reports should include:**

1. JiuwenSwarm version (`jiuwenswarm --version`)
2. OS and Python version
3. Reproduction steps
4. Expected behavior vs actual behavior
5. Relevant logs (`~/.jiuwenswarm/logs/` and `~/.jiuwenswarm/agent/.logs`)

### Submitting Pull Requests

1. **Fork the repo**: Fork [openJiuwen/jiuwenswarm](https://gitcode.com/openJiuwen/jiuwenswarm) on GitCode

2. **Create a branch**:

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

3. **Develop and test**: Run tests after making changes

```bash
uv run pytest tests/unit_tests/
```

4. **Commit your changes**:

```bash
git add .
git commit -m "feat: add XXX feature"
```

5. **Push and create a PR**:

```bash
git push origin feature/your-feature-name
```

Create a Pull Request on GitCode targeting the `develop` branch.

**PR description should include:**

- What changed and why
- Related Issue (e.g. `Closes #123`)
- How to test
- Screenshots or recordings (for UI changes)

### Sharing Skills

Publish your Skills to [Swarm Skills Hub](https://swarmskills.openjiuwen.com/) so more developers can discover, install, and reuse them.

---

## Development Environment Setup

For detailed setup instructions, see the [Developer Guide](developer_guide.md). Core steps:

```bash
# Clone the repository
git clone https://gitcode.com/openJiuwen/jiuwenswarm.git
cd jiuwenswarm

# Create virtual environment
uv venv --python=3.11
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
uv sync

# Initialize and start
uv run jiuwenswarm-init
uv run jiuwenswarm-start
```

---

## Code Standards

### Code Style

- **Python**: Follow [PEP 8](https://peps.python.org/pep-0008/), use type annotations
- **TypeScript / React**: Follow the project's ESLint configuration
- **Documentation**: Keep Chinese and English docs in sync

### Commit Message Convention

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>
```

| type | Description |
|------|-------------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation change |
| `style` | Code formatting (no functional change) |
| `refactor` | Refactoring (not a feature or fix) |
| `test` | Test cases |
| `chore` | Build, dependencies, tooling changes |

Examples:

```
feat(swarm): add Swarmflow stateful operator support
fix(config): fix model config not auto-restarting after save
docs: update Python version requirement in install guide
```

### Branch Naming Convention

| Prefix | Purpose | Example |
|--------|---------|---------|
| `feature/` | New feature | `feature/swarmflow-stateful-operator` |
| `fix/` | Bug fix | `fix/config-restart-issue` |
| `docs/` | Documentation update | `docs/update-install-guide` |
| `refactor/` | Code refactoring | `refactor/llm-client` |

---

## PR Review Process

1. **Automated checks**: Tests run automatically when a PR is created
2. **Code review**: At least two Committers must approve
3. **Revision feedback**: Address review comments and push updates
4. **Auto-merge**: After two approvals, openJiuwen-bot automatically merges into `develop`

**Review focus areas:**

- Code logic correctness
- Corresponding test coverage
- Impact on existing functionality
- Documentation updates

---

## Version Releases

- Development branch: `develop`
- Release branches: created from `develop` as `release/vX.Y.Z`
- Version numbers follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`
- After release, create Release Notes on [GitCode Releases](https://gitcode.com/openJiuwen/jiuwenswarm/releases)

---

## Contact

- **Issues**: [GitCode Issues](https://gitcode.com/openJiuwen/jiuwenswarm/issues)
- **Pull Requests**: [GitCode Pull Requests](https://gitcode.com/openJiuwen/jiuwenswarm/pulls)
- **Community**: Follow openJiuwen community events