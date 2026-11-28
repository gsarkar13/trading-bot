"""Import every implemented plugin so it registers itself. Add new plugins here."""

from app.strategies import ema200_hammer, pdh_pdl

PLUGIN_MODULES = (ema200_hammer, pdh_pdl)
