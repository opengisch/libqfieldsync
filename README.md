# libqfieldsync

This library facilitates packaging and synchronizing QGIS projects for use with [QField](http://www.qfield.org).

This library is the heart of QFieldSync QGIS plugin.

More information can be found in the [QField documentation](https://docs.qfield.org/get-started/).

The plugin can be download on the [QGIS plugin repository](https://plugins.qgis.org/plugins/qfieldsync/).


## Development

Improvements are welcome, feel free to fork and open a PR.

### Code style

Code style done with [pre-commit](https://pre-commit.com).

```
pip install pre-commit
# install pre-commit hook
pre-commit install
```

## Testing

```console
GITHUB_WORKSPACE=$PWD QGIS_TEST_VERSION=final-3_34_2 docker compose -f .docker/docker-compose.yml run qgis /usr/src/.docker/run-docker-tests.sh
```
