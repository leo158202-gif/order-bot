"""Pure candidate formatting. All permission and source checks live in broker."""
def transform(payload):
    response = payload.get('model_response', {})
    items = []
    for item in response.get('items', []):
        items.append({key: item[key] for key in ('name', 'quantity', 'unit', 'source_ids')})
    return {'items': items, 'explanation': ''}
