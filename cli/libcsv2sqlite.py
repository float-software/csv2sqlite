import os
import re
import bz2
import csv
import sys
import gzip
import json
import zipfile
import collections
import importlib.util
from operator import itemgetter

import dbutils
import transformations


def load_mapping_config(mapping_path):
    with open(mapping_path) as data_file:
        j = json.load(data_file)

        transforms = j['transformations'] if 'transformations' in j else None
        return (j['table_name'], transforms, j['mappings'])


def get_mappings_by_csv_index(mappings, index):
    the_mappings = []

    for mapping in mappings:
        if mapping['csv_index'] == index:
            the_mappings.append(mapping)

    return the_mappings


def import_csv(all_csv_data, table_name, mappings):
    keys = []

    for mapping in mappings:
        column_name = mapping['column_name']

        if 'key' in mapping and mapping['key'] == 'fk':
            column_name = column_name + '_id'

        keys.append(column_name)

    dbutils.insert_many(table_name, keys, all_csv_data)


def read_key_mappings(all_data, mappings):
    fk_mappings = []
    pk_mapping = None

    for mapping in mappings:
        if 'key' in mapping and mapping['key'] == 'fk':
            fk_mappings.append(mapping)

        elif 'key' in mapping and mapping['key'] == 'pk':
            pk_mapping = mapping

    for mapping in fk_mappings:
        mapping['dataset'] = set()

    for row in all_data:
        for mapping in fk_mappings:
            index = mapping['csv_index']
            mapping['dataset'].add(row[index])

    return fk_mappings, pk_mapping


def fk_mappings_to_database(fk_mappings):
    if not fk_mappings:
        return

    fk_patch_data = []

    for mapping in fk_mappings:
        # FK tables have the same name as FK column
        table_name = column_name = mapping['column_name']

        db_results = dbutils.select_all(column_name, ['id', 'value'])

        db_set = set()
        for result in db_results:
            db_set.add(result['value'])

        diff_set = mapping['dataset'] - db_set

        # Insert only new values in the database
        for value in diff_set:
            dbutils.insert(table_name, {'value': value})

        fk_patch_data.append({
            'csv_index': mapping['csv_index'],
            'db_values': dbutils.select_all(table_name, ['id', 'value']),
        })

    return fk_patch_data


def pk_table_to_database(pk_table):
    if not pk_table:
        return


def get_column_id(index, column, values):
    for value in values:
        if column == value['value']:
            return value['id']


def patch_csv_data(fk_patch_data, all_csv_data):
    if not fk_patch_data:
        return

    for row in all_csv_data:
        for fk_patch_item in fk_patch_data:
            index = fk_patch_item['csv_index']

            # Replace row value with respective id from new table
            row[index] = get_column_id(index, row[index], fk_patch_item['db_values'])


def set_mapping_defaults(all_csv_data, mappings, headers):
    def column_gen(column_id, row_count):
        for i in range(0, min(row_count, 1000)):
            yield all_csv_data[i][column_id]

    types = []
    column_count = len(all_csv_data[0])
    row_count = len(all_csv_data)

    for i in range(0, column_count):
        python_type = guess_column_type(column_gen(i, row_count))
        sqlite_type = dbutils.python_to_sqlite_type(python_type)
        types.append(sqlite_type)

    for mapping in mappings:
        i = mapping['csv_index']

        if 'data_type' not in mapping:
            mapping['data_type'] = types[i]

        if 'column_name' not in mapping:
            if len(headers) > 0:
                # Replace column name (do some cleaning first)
                mapping['column_name'] = re.sub('[^0-9a-zA-Z]+', '_', headers[i])
            else:
                mapping['column_name'] = 'column_' + str(i)

        if 'transform' not in mapping:
            mapping['transform'] = None
        else:
            mapping['transform'] = getattr(transformations, mapping['transform'])


def load_custom_transformations(mapping_path, custom_transformations_path):
    # use json path as reference
    path = os.path.abspath(os.path.dirname(mapping_path))

    # remove extension
    module_name = os.path.splitext(custom_transformations_path)[0]

    # custom loading stuff
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(path, custom_transformations_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for function_name in dir(module):
        # Remove internal stuff
        if function_name[0:2] != '__':
            func = getattr(module, function_name)
            setattr(transformations, function_name, func)


def get_data_type(value):
    value = value.strip()

    if value[0] == '+' or value[0] == '-':
        value = value[1:]

    if value.isdigit():
        return int

    if value.count(".") == 1 and value.replace(".", "", 1).isdigit():
        return float

    return str


def guess_column_type(generator):
    count = {
        int: 0,
        float: 0,
        str: 0
    }

    for val in generator:
        the_type = get_data_type(val)
        count[the_type] += 1

    if count[str] > 0:
        return str

    if count[float] > 0:
        return float

    return int


# TODO - Refactor
def uniquefy_names(mappings):
    names = {}

    for mapping in mappings:
        names[mapping['column_name']] = 0

    for mapping in mappings:
        count = names[mapping['column_name']]

        if count == 0:
            names[mapping['column_name']] += 1
        else:
            key = mapping['column_name'] + '_' + str(count)

            while key in names.keys():
                count += 1
                key = mapping['column_name'] + '_' + str(count)

            names[mapping['column_name']] = count + 1
            mapping['column_name'] = key


# TODO - Looks ineficient
def csv_transform(all_csv_data, mappings):
    new_csv_data = []

    for row in all_csv_data:
        new_row = []

        for mapping in mappings:
            val = row[mapping['csv_index']]

            if mapping['transform']:
                new_row.append(mapping['transform'](val))
            else:
                new_row.append(val)

        new_csv_data.append(new_row)

    return new_csv_data


def csv_read_file(csv_path, mappings):
    all_csv_data = []

    csv_file = open(csv_path, mode='r')

    reader = csv.reader(csv_file)

    for row in reader:
        # changed_row = csv_read_row(row, mappings)
        all_csv_data.append(row)

    csv_file.close()

    return all_csv_data


def csv_to_sqlite3(args):
    csv_path = args.input
    mapping_path = args.mapping
    db_path = args.output

    try:
        csv_has_title_columns = args.csv_has_title_columns
    except AttributeError:
        csv_has_title_columns = False

    # Load config
    table_name, custom_transformations, mappings = load_mapping_config(mapping_path)

    # Load custom transformations if they exist
    if custom_transformations:
        load_custom_transformations(mapping_path, custom_transformations)

    # Load csv file into a list
    all_csv_data = csv_read_file(csv_path, mappings)

    headers = []

    if csv_has_title_columns:
        # Remove headers
        headers = all_csv_data[0]
        all_csv_data = all_csv_data[1:]

    dbutils.create_and_connect(db_path)

    # Set mapping defaults
    set_mapping_defaults(all_csv_data, mappings, headers)

    all_csv_data = csv_transform(all_csv_data, mappings)

    # Create database table
    dbutils.create_table(table_name, mappings)

    # Load fk tables
    fk_mappings, _ = read_key_mappings(all_csv_data, mappings)

    fk_patch_data = fk_mappings_to_database(fk_mappings)

    # Substitute data with foreign key IDs
    patch_csv_data(fk_patch_data, all_csv_data)

    # Import result
    import_csv(all_csv_data, table_name, mappings)
