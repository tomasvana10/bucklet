# bucklet

Bucklet is a small CLI/TUI tool for managing objects in S3 compatible buckets, with support for all storage classes.

## Install

Python >=3.10 is required.

1. Install the `bucklet` package from PyPI:

```bash
# using pipx
pipx ensurepath && pipx install bucklet

# using uv
uv tool install bucklet
```

2. Optionally, install shell completion:

```bash
# fish
register-python-argcomplete --shell fish bucklet > ~/.config/fish/completions/bucklet.fish

# bash
echo 'eval "$(register-python-argcomplete bucklet)"' >> ~/.bashrc

# zsh
echo 'autoload -U compinit bashcompinit && compinit && bashcompinit; eval "$(register-python-argcomplete bucklet)"' >> ~/.zshrc

# PowerShell
Add-Content $PROFILE 'register-python-argcomplete --shell powershell bucklet | Out-String | Invoke-Expression'
```

3. Run `bucklet` to open the TUI and configure a profile with your S3 credentials.

> [!IMPORTANT]
> Deletion of objects is only supported through the TUI when launched with the `--allow-deletion` flag.

## Usage

```
bucklet [-h] [--profile NAME] [--allow-deletion] {up,get,thaw,ls,stat,profile} ...

positional arguments:
  {up,get,thaw,ls,stat,profile}
    up                  upload files/dirs (mirrors absolute path)
    get                 download objects (globs allowed)
    thaw                thaw archived objects (globs allowed)
    ls                  list objects
    stat                show detailed status of objects (globs allowed)
    profile             manage saved profiles

options:
  -h, --help            show this help message and exit
  --profile NAME        profile to use (a saved name, or a raw bucket name). Defaults to the configured default profile
  --allow-deletion      allow deleting objects in the TUI (no effect on the subcommands)
```

## Development

Set up the environment:

```bash
uv sync
pre-commit install
```

Test the project:

```bash
uv run pytest
uv run pytest --cov=bucklet
```

Run the project:

```bash
uv run bucklet
```
