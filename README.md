# Parameter Sweep

## For contributors

### Installation

```sh
# create Conda env for Parameter Sweep development, if not present
conda create --yes --name parameter-sweep-dev python=3.10
# ensure Conda env is active
conda activate parameter-sweep-dev
# clone this repo locally
git clone https://github.com/watertap-org/parameter-sweep && cd parameter-sweep
pip install -r requirements-dev.txt
```

### Running tests

```sh
conda activate parameter-sweep-dev
pytest --pyargs parameter_sweep
```

### Before committing

```sh
black .
```