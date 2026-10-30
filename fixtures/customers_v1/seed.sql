-- Synthetic data only.
PRAGMA user_version = 1;
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    subscription_status TEXT NOT NULL CHECK (subscription_status IN ('active', 'inactive', 'cancelled'))
);
CREATE TABLE plans (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    monthly_cents INTEGER NOT NULL
);
INSERT INTO customers (id, name, email, subscription_status) VALUES
    (1, 'Ada Fixture',      'ada@example.test',      'active'),
    (2, 'Brook Sample',     'brook@example.test',    'cancelled'),
    (3, 'Casey Placeholder','casey@example.test',    'inactive'),
    (4, 'Devon Synthetic',  'devon@example.test',    'active'),
    (5, 'Emery Mock',       'emery@example.test',    'inactive'),
    (6, 'Finley Testcase',  'finley@example.test',   'active');
INSERT INTO plans (id, code, monthly_cents) VALUES (1, 'basic', 900), (2, 'pro', 2900);
