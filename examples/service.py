import datetime
import logging

from nameko_openapi import OpenApi


PETS = {}
next_pet_id = 1

NoContent = ''


class PetstoreService(object):
    name = 'petstore'

    api = OpenApi('petstore.yaml')


    @api.operation('get_pets')
    def get_pets(self, limit, animal_type=None):
        pets = []
        for pet in PETS.values():
            if animal_type is None or pet.animal_type == animal_type:
                pets.append(pet)
        return {"pets": pets[:limit]}

    #@api.operation('post_pet', body_name='pet')
    @api.operation('post_pet')
    def post_pet(self, pet):
        global next_pet_id
        pet.__dict__.update({
            'id': str(next_pet_id),
            'created': datetime.datetime.utcnow(),
        })
        logging.info('Creating pet %s..', pet.id)
        PETS[pet.id] = pet
        next_pet_id += 1
        return 201, NoContent

    @api.operation('get_pet')
    def get_pet(self, pet_id):
        pet = PETS.get(pet_id)
        return pet or (404, 'Not found')

    @api.operation('put_pet', body_name='pet')
    def put_pet(self, pet_id, pet):
        logging.info('Updating pet %s..', pet_id)
        PETS[pet_id].__dict__.update(pet.__dict__)
        return 200, NoContent

    @api.operation('delete_pet')
    def delete_pet(self, pet_id):
        if pet_id in PETS:
            logging.info('Deleting pet %s..', pet_id)
            del PETS[pet_id]
            return 204, NoContent
        else:
            return 404, NoContent

