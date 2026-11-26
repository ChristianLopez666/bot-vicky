"""Cliente de Google Sheets con memoria acotada.

googleapiclient construye un recurso nuevo cada vez que se llama
`service.spreadsheets()`, y genera con él la documentación de todos sus métodos:
unos 26 MB temporales por llamada (medido con google-api-python-client 2.202.0).
Quedan en ciclos que el recolector tarda en liberar, y en una instancia de
512 MB eso termina en reinicios por memoria. Aquí el recurso se crea una sola vez
por cliente y se reutiliza; `values()` sobre ese recurso no tiene ese costo.

Cada hilo debe seguir usando su propio cliente: httplib2 no es seguro entre hilos.
"""
import gc


def reuse_spreadsheets(service):
    """Hace que `service.spreadsheets()` devuelva siempre el mismo recurso."""
    resource = service.spreadsheets()
    service.spreadsheets = lambda: resource
    gc.collect()                     # libera de una vez lo temporal de esa creación
    return service
