import config
from .bot import Lookout

Lookout().run(config.token, root_logger=True)
