"""Build1 R12 presentation only. Never use these rounded values for pricing or exports.

Percent input is percentage points, not a ratio. Legacy exact facts are untouched.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import math

MINUS='−'
NBSP='\u00a0'

def _rounded(value, places):
    if isinstance(value,bool) or not isinstance(value,(str,int,float,Decimal)) or not isinstance(places,int) or not 0<=places<=6:
        return None
    try:
        # Build1's reference removes binary noise first, then rounds halves away.
        numeric=float(value)
        if not math.isfinite(numeric):return None
        normalized=Decimal(repr(round(numeric,6)))
        with localcontext() as context:
            context.prec=max(40,len(normalized.as_tuple().digits)+max(0,normalized.adjusted())+places+8)
            result=normalized.quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_UP)
        return result.copy_abs() if result==0 else result
    except (InvalidOperation,ValueError,TypeError,OverflowError):
        return None

def number(value, places=1, language='pl', *, signed=False):
    result=_rounded(value,places)
    if result is None:return None
    text=format(abs(result),f',.{places}f')
    if str(language).startswith('pl'):
        text=text.replace(',',NBSP).replace('.',',')
    sign=MINUS if result<0 else '+' if signed and result>0 else '±' if signed else ''
    return sign+text

def _unknown(language):return 'Brak danych' if str(language).startswith('pl') else 'No data'

def energy(value, *, role='tile', period='day', language='pl'):
    result=number(value,2 if role=='table' and period=='day' else 1,language)
    return result+' kWh' if result is not None else _unknown(language)

def money(value, currency, *, language='pl'):
    result=number(value,2,language)
    unit='zł' if str(language).startswith('pl') and currency=='PLN' else currency
    return result+' '+unit if result is not None and isinstance(unit,str) and unit else _unknown(language)

def rate(value, currency, *, language='pl'):
    result=money(value,currency,language=language)
    return result+'/kWh' if result!=_unknown(language) else result

def percent(value, *, role='share', language='pl'):
    result=number(value,1 if role=='change' else 0,language,signed=role=='change')
    return result+'%' if result is not None else _unknown(language)

def plural(value, one, few, many, language='pl'):
    try:
        n=abs(float(value))
        if not math.isfinite(n):return many
        if not str(language).startswith('pl'):return one if n==1 else many
        if not n.is_integer():return few
        n=int(n)
        return one if n==1 else few if 2<=n%10<=4 and not 12<=n%100<=14 else many
    except (ValueError,TypeError,OverflowError):return many
