from .etherbone import add_etherbone
from .spi import add_spi

# Mapping with connection type and the function which should
# be called in that case
CONNECTION_MAPPING = {
    "etherbone": add_etherbone,
    "spi": add_spi
}


def add_connection(soc, config):
    for connection in config.connections:
        if connection.connection_type not in CONNECTION_MAPPING:
            raise KeyError(f"Unknown connection type '{config.connection.connection_type}'")
        CONNECTION_MAPPING[connection.connection_type](soc, connection)


__all__ = [
    'add_connection',
]