import csv
from collections import defaultdict
from datetime import datetime

def read_csv_file(file_path):
    data = defaultdict(list)
    with open(file_path, mode='r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            data['Artist'].append(row['Artist'])
            data['Album'].append(row['Album'])
            data['Year'].append(row['Year'])
    return data

def sort_data_by_artist(data):
    # Combine the data into a list of tuples and sort by Artist
    combined_data = list(zip(data['Artist'], data['Album'], data['Year']))
    combined_data.sort(key=lambda x: x[0])
    # Unzip the sorted data back into separate lists
    data['Artist'], data['Album'], data['Year'] = zip(*combined_data)
    return data

def write_markdown_table(data, output_file):
    

    with open(output_file, mode='w', encoding='utf-8') as file:
        file.write("# George's CD Collection\n\n")
        file.write(f"This is George's CD collection. It has last been update on the {datetime.today().strftime('%d/%m/%Y')} \n\n")
        file.write("| Artist | Album | Year |\n")
        file.write("|--------|-------|------|\n")
        for artist, album, year in zip(data['Artist'], data['Album'], data['Year']):
            file.write(f"| {artist} | {album} | {year} |\n")

def main():
    input_file = 'collection.csv' 
    output_file = 'README.md'  

    data = read_csv_file(input_file)
    sorted_data = sort_data_by_artist(data)
    write_markdown_table(sorted_data, output_file)

if __name__ == "__main__":
    main()
