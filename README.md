# libqfieldsync

This library facilitates packaging and synchronizing QGIS projects for use with [QField](http://www.qfield.org).

This library is the heart of the QFieldSync QGIS plugin and QFieldCloud's QGIS worker container.

More information can be found in the [QField documentation](https://docs.qfield.org/get-started/).

The QFieldSync plugin can be downloaded on the [QGIS plugin repository](https://plugins.qgis.org/plugins/qfieldsync/).


## Development

Improvements are welcome, feel free to fork and open a PR.


### Getting started

This project uses [`uv`](https://docs.astral.sh/uv/getting-started/installation/) for managing it's dependencies.

```shell
git clone git@github.com:opengisch/libqfieldsync.git
cd libqfieldsync

# we need to pass `system-site-packages` to use the local QGIS version
uv venv --system-site-packages

# we use `pre-commit` for code styling, see https://pre-commit.com
uv run pre-commit install
```

## Testing

Run local tests (assuming a QGIS installed on host):

```shell
uv run pytest
```

If you want to test with a specific QGIS version, or you don't have QGIS installed, then:

```shell
docker run --rm $(docker build --build-arg QGIS_TEST_VERSION=ltr -q -f .docker/Dockerfile .) .docker/xvfb-pytest
```
