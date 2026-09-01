import json

# Load JSON directly into a single variable
database_queries = json.load(open("queries.json"))

# Iterate through the variable and print keys and query values
for category, queries in database_queries.items():
    print(f"\n=== CATEGORY: {category.upper()} ===")
    for key, sql in queries.items():
        print(f"\nKey: {key}\nQuery:\n{sql}")