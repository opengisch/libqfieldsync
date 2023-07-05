import logging

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QObject, pyqtSignal


def addLoggingLevel(level_name, levelno, method_name=None):
    """
    Comprehensively adds a new logging level to the `logging` module and the
    currently configured logging class.

    `levelName` becomes an attribute of the `logging` module with the value
    `levelNum`. `method_name` becomes a convenience method for both `logging`
    itself and the class returned by `logging.getLoggerClass()` (usually just
    `logging.Logger`). If `method_name` is not specified, `levelName.lower()` is
    used.

    To avoid accidental clobberings of existing attributes, this method will
    raise an `AttributeError` if the level name is already an attribute of the
    `logging` module or if the method name is already present

    Example
    -------
    >>> addLoggingLevel('TRACE', logging.DEBUG - 5)
    >>> logging.getLogger(__name__).setLevel("TRACE")
    >>> logging.getLogger(__name__).trace('that worked')
    >>> logging.trace('so did this')
    >>> logging.TRACE
    5

    Shamelessly adapted from: https://stackoverflow.com/a/35804945
    """
    if not method_name:
        method_name = level_name.lower()

    if hasattr(logging, level_name):
        return

    if hasattr(logging.getLoggerClass(), method_name):
        raise AttributeError("{} already defined in logger class".format(method_name))

    # This method was inspired by the answers to Stack Overflow post
    # http://stackoverflow.com/q/2183233/2988730, especially
    # http://stackoverflow.com/a/13638084/2988730
    def logForLevel(self, message, *args, **kwargs):
        if self.isEnabledFor(levelno):
            self._log(levelno, message, args, **kwargs)

    logging.addLevelName(levelno, level_name)
    setattr(logging, level_name, levelno)
    setattr(logging.getLoggerClass(), method_name, logForLevel)


# add QGIS success log level
addLoggingLevel("SUCCESS", logging.DEBUG - 5)

if Qgis.QGIS_VERSION_INT >= 32000:
    LogNoLevel = Qgis.MessageLevel.NoLevel
else:
    LogNoLevel = getattr(Qgis.MessageLevel, "None")

_pythonLevelToQgisLogLevel = {
    logging.CRITICAL: Qgis.MessageLevel.Critical,
    logging.ERROR: Qgis.MessageLevel.Critical,
    logging.WARNING: Qgis.MessageLevel.Warning,
    logging.INFO: Qgis.MessageLevel.Info,
    logging.DEBUG: Qgis.MessageLevel.Info,
    logging.SUCCESS: Qgis.MessageLevel.Success,  # type: ignore
    logging.NOTSET: LogNoLevel,
}


class QgisLogObserver(QObject):
    log_signal = pyqtSignal(str, str)

    def emit(self, level: str, message: str) -> None:
        self.log_signal.emit(level, message)


class QgisLogHandler(logging.Handler):
    source = "libqfieldsync"

    def __init__(self, qgis_log_observer: QgisLogObserver, *args, **kwargs) -> None:
        self.qgis_log_observer = qgis_log_observer
        super().__init__(*args, **kwargs)

    def _get_qgis_log_level(self, record: logging.LogRecord) -> int:
        return _pythonLevelToQgisLogLevel.get(record.levelno, LogNoLevel)

    def emit(self, record):
        try:
            msg = self.format(record)
            qgis_log_level = self._get_qgis_log_level(record)

            QgsMessageLog.logMessage(msg, self.source, qgis_log_level)

            self.qgis_log_observer.emit(self.source, msg)
        except RecursionError:  # See issue 36272
            raise
        except Exception:
            self.handleError(record)


qgis_log_observer = QgisLogObserver()
qgis_log_handler = QgisLogHandler(qgis_log_observer)

logger_exists = bool(logging.Logger.manager.loggerDict.get("libqfieldsync"))
logger = logging.getLogger("libqfieldsync")

if not logger_exists:
    logger.setLevel(logging.DEBUG)
    logger.addHandler(qgis_log_handler)
