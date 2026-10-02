"""The provider layer: one platform's API mapped onto the DocProvider contract.

``provider.py`` is the contract and the shared types; each ``*_provider.py``
implements it for one platform, its ``*_formats.py`` companion handles the
document kinds that platform reaches beyond its native doc, and ``factory.py``
/ ``routing.py`` / ``kinds.py`` pick and address the right one.
"""
