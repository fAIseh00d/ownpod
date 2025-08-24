import logging
from config.settings import LOG_LEVEL

# Setup logging
def setup_logger():
    logger = logging.getLogger("ownpod")
    if not logger.handlers:
        _handler = logging.StreamHandler()
        _formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        _handler.setFormatter(_formatter)
        logger.addHandler(_handler)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    return logger

# Create logger instance
logger = setup_logger()
