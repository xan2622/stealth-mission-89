pub mod font;
pub mod terrain;
pub mod wld;

/// Theater codes in the order the game uses for file names.
pub const THEATERS: [&str; 9] = ["LB", "PG", "NC", "CE", "VN", "ME", "KU", "CU", "KO"];

/// All theater codes: the nine classic theaters, then the remake's own (their data exists
/// only in the shipped assets). UO is a test copy of UA with Kharkiv's buildings from
/// OpenStreetMap.
pub const ALL_THEATERS: [&str; 11] =
    ["LB", "PG", "NC", "CE", "VN", "ME", "KU", "CU", "KO", "UA", "UO"];
