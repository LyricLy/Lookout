class BadLogError(Exception):
    pass

class InvalidHTMLError(BadLogError):
    pass

class UnsupportedRoleError(BadLogError):
    pass

class NotLogError(BadLogError):
    pass
