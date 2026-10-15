CREATE TABLE exposure_scenarios (
 position_id INTEGER PRIMARY KEY REFERENCES positions(id),
 legs_json TEXT NOT NULL,
 scenarios_json TEXT NOT NULL,
 note TEXT NOT NULL
);
