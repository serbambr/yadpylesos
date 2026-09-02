from yadpylesos import BaseCloudProvider


class MailAPIService(BaseCloudProvider):
    """Заглушка для будущей поддержки Mail.ru"""
    def __init__(self, app):
        super().__init__(app)
        raise NotImplementedError("Поддержка Mail.ru находится в разработке (Долгосрочная перспектива)")
