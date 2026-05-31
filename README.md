# bucklet

Bucklet is a small CLI/TUI tool for managing objects in S3 compatible buckets.

## Install

Python >=3.10 required

1. `pip install bucklet`

2. Optionally, install shell completion:

```sh
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

## Usage

```
bucklet [-h] [--profile NAME] {up,get,thaw,ls,stat,profile} ...

positional arguments:
  {up,get,thaw,ls,stat,profile}
    up                  upload files/dirs (mirrors absolute path)
    get                 download objects (globs allowed)
    thaw                restore archived objects (globs allowed)
    ls                  list objects
    stat                show detailed status of objects (globs allowed)
    profile             manage saved profiles

options:
  -h, --help            show this help message and exit
  --profile NAME        profile to use (a saved name, or a raw bucket name); defaults to the configured default profile
```

## Development

- Set up the environment:

```bash
uv sync
pre-commit install
```

- Test the project:

```bash
uv run pytest
uv run pytest --cov=bucklet
```

- Run the project:

```bash
uv run bucklet
```
