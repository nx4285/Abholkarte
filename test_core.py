import os, tempfile
os.environ['KLAZ_API_KEY']=''
from app import extract_ad_id, parse_search_url, clean_url

def test_id():
    u='https://www.kleinanzeigen.de/s-anzeige/foo/3473515953-216-5935?utm_source=x'
    assert extract_ad_id(u)=='3473515953'
    assert clean_url(u).endswith('/3473515953-216-5935')

def test_search():
    x=parse_search_url('https://www.kleinanzeigen.de/s-autos/skoda-citigo/k0c216l1234r50')
    assert x['query']=='skoda citigo' and x['category_id']=='216' and x['location_id']=='1234' and x['distance']==50

if __name__=='__main__': test_id(); test_search(); print('OK')
