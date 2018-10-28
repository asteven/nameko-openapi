### Run examples

```
cd /path/to/namkeo-openapi/examples

nameko run --config ./config.yaml service
```

### Poke it

```
curl -H 'Content-Type: application/json' -X POST --data '{"name": "Maui", "animal_type": "cat"}' 'http://127.0.0.1:8001/pets'
curl -H 'Content-Type: application/json' -X POST --data '{"name": "Pussy", "animal_type": "cat"}' 'http://127.0.0.1:8001/pets'
curl -H 'Content-Type: application/json' -X POST --data '{"name": "Bello", "animal_type": "dog"}' 'http://127.0.0.1:8001/pets'

curl 'http://127.0.0.1:8001/pets'

curl 'http://127.0.0.1:8001/pets?limit=1'

curl 'http://127.0.0.1:8001/pets?anymal_type=dog'

```
