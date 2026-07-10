"""Diet/nutrition tools, ported from langchain_diet_agent/app.py.

Tool logic is unchanged from the diet agent; only the module layout is new.
Data grounding: local FoodData Central subset (JSON) and recipe SQLite DB.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr
from langchain.tools import BaseTool
from duckduckgo_search import DDGS

from healthva.common import FDC_PATH, RECIPES_DB_PATH


# --- Args schemas ---

class BmrTdeeArgs(BaseModel):
    age: Optional[str] = Field(default=None, description="Age in years (e.g. '30')")
    sex: Optional[Literal["male", "female"]] = Field(default=None, description="Biological sex")
    height_cm: Optional[str] = Field(default=None, description="Height in centimeters (e.g. '175')")
    weight_kg: Optional[str] = Field(default=None, description="Weight in kilograms (e.g. '70')")
    activity_level: Optional[
        Literal["sedentary", "light", "moderate", "very_active", "extra_active"]
    ] = Field(default=None, description="Activity level")
    goal: Optional[Literal["lose_weight", "maintain_weight", "gain_weight"]] = Field(
        default=None, description="Weight goal"
    )


class FoodLookupArgs(BaseModel):
    query: str = Field(..., description="Food name or partial name, e.g. 'boiled egg'")
    max_results: str = Field(default="5", description="Maximum results to return, e.g. '5'")


class RecipeSearchArgs(BaseModel):
    query: str = Field(..., description="Dish or ingredient keywords")
    max_results: str = Field(default="5", description="Maximum results to return, e.g. '5'")
    exclude_ingredients: Optional[List[str]] = Field(
        default=None, description="Ingredients to exclude"
    )
    must_include_ingredients: Optional[List[str]] = Field(
        default=None, description="Ingredients that must be present"
    )
    dietary_restrictions: Optional[List[
        Literal["vegetarian", "vegan", "pescatarian", "gluten_free", "dairy_free"]
    ]] = Field(default=None, description="Dietary restrictions to apply")


class WebSearchArgs(BaseModel):
    query: str = Field(..., description="Search query for recipes or nutrition info")
    max_results: str = Field(default="5", description="Maximum results, e.g. '5'")


class UnitConvertArgs(BaseModel):
    amount: str = Field(..., description="Numeric amount to convert (e.g. '150')")
    from_unit: str = Field(..., description="Source unit, e.g., 'lb', 'cup', 'oz'")
    to_unit: str = Field(..., description="Target unit, e.g., 'g', 'ml'")
    food: Optional[str] = Field(
        default=None,
        description="Optional food item for specific weights (e.g., apple, egg)",
    )


# --- Unit conversion ---

CONVERSIONS = {
    # Volume conversions (to ml)
    "cup": {"ml": 240, "tbsp": 16, "tsp": 48},
    "tbsp": {"ml": 15, "tsp": 3},
    "tsp": {"ml": 5},
    "ml": {"cup": 1 / 240, "tbsp": 1 / 15, "tsp": 1 / 5},
    "l": {"ml": 1000, "cup": 4.17},
    # Weight conversions (to grams)
    "g": {"kg": 0.001, "oz": 0.035, "lb": 0.002},
    "kg": {"g": 1000, "oz": 35.27, "lb": 2.205},
    "oz": {"g": 28.35, "kg": 0.028, "lb": 0.063},
    "lb": {"g": 453.6, "kg": 0.454, "oz": 16},
    # Length conversions (to cm)
    "cm": {"in": 0.3937, "ft": 0.0328, "m": 0.01},
    "m": {"cm": 100, "in": 39.37, "ft": 3.28},
    "in": {"cm": 2.54, "ft": 1 / 12, "m": 0.0254},
    "ft": {"in": 12, "cm": 30.48, "m": 0.3048},
    # Food-specific weights (approximate)
    "apple": {"g": 182},
    "banana": {"g": 118},
    "orange": {"g": 140},
    "egg": {"g": 50},
    "slice_bread": {"g": 25},
    "tbsp_butter": {"g": 14},
    "cup_rice": {"g": 185},
    "cup_pasta": {"g": 140},
}


def unit_convert(amount: float, from_unit: str, to_unit: str, food: Optional[str] = None) -> float:
    """Convert between kitchen units (volume, weight, and food-specific)."""
    unit_map = {
        "grams": "g", "gram": "g",
        "kilograms": "kg", "kilogram": "kg", "kgs": "kg",
        "ounces": "oz", "ounce": "oz",
        "pounds": "lb", "pound": "lb", "lbs": "lb",
        "milliliters": "ml", "millilitre": "ml",
        "liters": "l", "litere": "l", "liquid_ounce": "oz",
        "cups": "cup",
        "tablespoons": "tbsp", "tablespoon": "tbsp",
        "teaspoons": "tsp", "teaspoon": "tsp",
        "inches": "in", "inch": "in",
        "feet": "ft", "foot": "ft",
        "meters": "m", "meter": "m",
        "centimeters": "cm", "centimeter": "cm",
    }

    f_unit = from_unit.lower().rstrip("s")
    t_unit = to_unit.lower().rstrip("s")
    f_unit = unit_map.get(from_unit.lower(), f_unit)
    t_unit = unit_map.get(to_unit.lower(), t_unit)

    if food and food.lower() in CONVERSIONS:
        food_key = food.lower()
        if f_unit == food_key and t_unit == "g":
            return amount * CONVERSIONS[food_key]["g"]
        if f_unit == "g" and t_unit == food_key:
            return amount / CONVERSIONS[food_key]["g"]

    try:
        if isinstance(amount, str):
            amt_str = amount.lower().replace("’", "'").replace("”", '"').replace("‘", "'").replace("“", '"').replace(",", ".")
            fi_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:feet|ft|foot|\'|")\s*(\d+(?:\.\d+)?)\s*(?:inches|in|inch|"|\')?', amt_str)
            if fi_match:
                feet = float(fi_match.group(1))
                inches = float(fi_match.group(2))
                amount = (feet * 12) + inches
                f_unit = "in"
            else:
                amount = float(amt_str)
        else:
            amount = float(amount)
    except (ValueError, TypeError):
        raise ValueError(
            f"Invalid amount: {amount}. Must be a number or formatted clearly like '5 feet 4 inches'."
        )

    if f_unit == t_unit:
        return amount
    if f_unit in CONVERSIONS and t_unit in CONVERSIONS[f_unit]:
        return amount * CONVERSIONS[f_unit][t_unit]
    if t_unit in CONVERSIONS and f_unit in CONVERSIONS[t_unit]:
        return amount / CONVERSIONS[t_unit][f_unit]
    raise ValueError(
        f"Cannot convert from {from_unit} to {to_unit}. Supported units: {list(CONVERSIONS.keys())}"
    )


def web_search(query: str, max_results: int = 5) -> Dict:
    """DuckDuckGo search for recipe/nutrition text snippets."""
    try:
        with DDGS() as ddgs:
            results: List[Dict[str, str]] = []
            for result in ddgs.text(query, max_results=max_results):
                results.append(
                    {
                        "title": result.get("title", ""),
                        "url": result.get("href", ""),
                        "snippet": result.get("body", ""),
                    }
                )
            return {"results": results}
    except Exception as exc:
        return {"results": [], "error": f"Search failed: {exc}"}


# --- Tools ---

class WebSearchTool(BaseTool):
    name: ClassVar[str] = "web_search"
    description: ClassVar[str] = (
        "Use DuckDuckGo text search to fetch recipe or nutrition info when the local "
        "database lacks coverage. Returns only titles, URLs, and snippets (no code)."
    )
    args_schema: ClassVar[type[WebSearchArgs]] = WebSearchArgs

    def _run(self, query: str, max_results: Any = 5) -> str:
        try:
            max_results = int(max_results)
        except (ValueError, TypeError):
            max_results = 5
        results = web_search(query=query, max_results=max_results)
        if results.get("error"):
            return f"Web search error: {results['error']}"
        hits = results.get("results", [])
        if not hits:
            return "No web results found. Try rephrasing the query."
        lines = []
        for item in hits:
            lines.append(
                f"Title: {item.get('title', '')}\nURL: {item.get('url', '')}\nSnippet: {item.get('snippet', '')}"
            )
            lines.append("---")
        return "\n".join(lines).strip()


class UnitConvertTool(BaseTool):
    name: ClassVar[str] = "unit_convert"
    description: ClassVar[str] = (
        "Convert quantities between kitchen units (g, kg, oz, lb, ml, cup, tbsp, tsp), "
        "length units (cm, m, in, ft), and common food-specific weights (apple, egg, etc.). "
        "Use this for metric/imperial conversion."
    )
    args_schema: ClassVar[type[UnitConvertArgs]] = UnitConvertArgs

    def _run(self, amount: Any, from_unit: str, to_unit: str, food: Optional[str] = None) -> str:
        try:
            converted = unit_convert(amount, from_unit, to_unit, food)
        except Exception as exc:
            return (
                f"Error: {exc}. Please ask the user to clarify their input "
                "(e.g., 'Could you please format your measurement clearly, like 165 cm or 5 feet 4 inches?')."
            )
        food_suffix = f" for {food}" if food else ""
        return f"{amount} {from_unit} = {converted:.2f} {to_unit}{food_suffix}"


class BmrTdeeTool(BaseTool):
    name: ClassVar[str] = "bmr_tdee_calculator"
    description: ClassVar[str] = (
        "Estimate BMR and TDEE using the Mifflin-St Jeor equation for adults. "
        "This is not medical advice. Requires age, sex, height_cm, and weight_kg. "
        "Optionally takes activity_level and goal."
    )
    args_schema: ClassVar[type[BmrTdeeArgs]] = BmrTdeeArgs

    def _run(
        self,
        age: Optional[Any] = None,
        sex: Optional[str] = None,
        height_cm: Optional[Any] = None,
        weight_kg: Optional[Any] = None,
        activity_level: Optional[str] = None,
        goal: Optional[str] = None,
    ) -> str:
        try:
            if age is not None:
                age = int(age)
            if height_cm is not None:
                height_cm = float(height_cm)
            if weight_kg is not None:
                weight_kg = float(weight_kg)
        except (ValueError, TypeError):
            return "Error: Age, height, and weight must be numeric. Please ask the user to clarify these values if they are unclear."

        missing = []
        if age is None:
            missing.append("age")
        if sex is None:
            missing.append("sex")
        if height_cm is None:
            missing.append("height_cm")
        if weight_kg is None:
            missing.append("weight_kg")
        if missing:
            return f"Error: Missing required fields: {missing}. Ask the user to provide these values to calculate BMR/TDEE."

        if sex not in ("male", "female"):
            return "Error: sex must be 'male' or 'female'. Please ask the user to clarify."

        if sex == "male":
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
        else:
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

        activity_multipliers = {
            "sedentary": 1.2,
            "light": 1.375,
            "moderate": 1.55,
            "very_active": 1.725,
            "extra_active": 1.9,
        }
        multiplier = activity_multipliers.get(activity_level or "sedentary", 1.2)
        tdee = bmr * multiplier

        goal_note = ""
        if goal == "lose_weight":
            goal_note = "For weight loss, people often target about 300-500 kcal/day below TDEE."
        elif goal == "gain_weight":
            goal_note = "For weight gain, people often target about 300-500 kcal/day above TDEE."
        elif goal == "maintain_weight":
            goal_note = "For weight maintenance, people often aim to stay near their TDEE."

        return (
            f"BMR (Mifflin-St Jeor) ~ {bmr:.0f} kcal/day.\n"
            f"TDEE (activity_level={activity_level or 'sedentary'}) ~ {tdee:.0f} kcal/day.\n\n"
            "These are rough estimates for generally healthy adults and are NOT medical advice.\n"
            + (goal_note or "")
        )


class FoodLookupTool(BaseTool):
    name: ClassVar[str] = "food_lookup"
    description: ClassVar[str] = (
        "Look up foods from a local FoodData Central subset and return approximate calories and macros "
        "per 100 g (or a standard serving). Use this instead of guessing nutritional values."
    )
    args_schema: ClassVar[type[FoodLookupArgs]] = FoodLookupArgs

    _fdc_path: Path = PrivateAttr()
    _foods: List[Dict[str, Any]] = PrivateAttr(default_factory=list)

    def __init__(self, fdc_path: Path = FDC_PATH):
        super().__init__()
        self._fdc_path = fdc_path
        self._load_data()

    def _load_data(self):
        if not self._fdc_path.exists():
            self._foods = []
            return
        with self._fdc_path.open("r", encoding="utf-8") as f:
            self._foods = json.load(f)

    def _run(self, query: str, max_results: Any = 5) -> str:
        try:
            max_results = int(max_results)
        except (ValueError, TypeError):
            max_results = 5

        q_tokens = set(re.findall(r"[a-z]+", query.lower()))
        if not q_tokens:
            return "Error: Query must contain at least one alphabetic character. Please ask the user to clarify."

        def score(food: Dict[str, Any]) -> int:
            text = f"{food.get('description', '')} {' '.join(food.get('tags', []))}".lower()
            f_tokens = set(re.findall(r"[a-z]+", text))
            return len(q_tokens & f_tokens)

        scored = [(score(food), food) for food in self._foods]
        scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [f for _, f in scored[:max_results]]

        if not top:
            return "No matching foods found in the local FDC subset."

        lines = []
        for food in top:
            lines.append(
                "Name: {desc}\n"
                "Category: {cat}\n"
                "Serving: {serv} g\n"
                "Macros: {kcal} kcal, {p} g protein, {f} g fat, {c} g carbs, {fib} g fiber, {sug} g sugar\n"
                "FDC ID: {fdc_id}".format(
                    desc=food.get("description", "Unknown"),
                    cat=food.get("category", "Unknown"),
                    serv=food.get("serving_size_g", 100),
                    kcal=food.get("calories_kcal", "?"),
                    p=food.get("protein_g", "?"),
                    f=food.get("fat_g", "?"),
                    c=food.get("carbs_g", "?"),
                    fib=food.get("fiber_g", "?"),
                    sug=food.get("sugar_g", "?"),
                    fdc_id=food.get("fdc_id", "?"),
                )
            )
            lines.append("---")
        return "\n".join(lines).strip()


class RecipeSearchTool(BaseTool):
    name: ClassVar[str] = "recipe_search"
    description: ClassVar[str] = (
        "Search recipes from a local SQLite database by title and ingredients. "
        "Can filter by ingredient keywords and simple dietary restrictions."
    )
    args_schema: ClassVar[type[RecipeSearchArgs]] = RecipeSearchArgs

    _db_path: Path = PrivateAttr()

    def __init__(self, db_path: Path = RECIPES_DB_PATH):
        super().__init__()
        self._db_path = db_path

    def _matches_diet(self, ingredients_text: str, dietary_restrictions: Optional[List[str]]) -> bool:
        if not dietary_restrictions:
            return True
        text = ingredients_text.lower()
        meat_words = ["chicken", "beef", "pork", "bacon", "ham", "lamb", "turkey", "duck", "sausage"]
        fish_words = ["fish", "shrimp", "salmon", "tuna", "cod", "tilapia", "crab", "lobster"]
        dairy_words = ["milk", "cheese", "butter", "yogurt", "cream", "whey"]
        gluten_words = ["wheat", "barley", "rye", "bread", "pasta", "flour", "noodle"]

        for dr in dietary_restrictions:
            if dr == "vegetarian":
                if any(w in text for w in meat_words + fish_words):
                    return False
            elif dr == "vegan":
                if any(w in text for w in meat_words + fish_words + dairy_words + ["egg", "honey"]):
                    return False
            elif dr == "pescatarian":
                if any(w in text for w in meat_words):
                    return False
            elif dr == "gluten_free":
                if any(w in text for w in gluten_words):
                    return False
            elif dr == "dairy_free":
                if any(w in text for w in dairy_words):
                    return False
        return True

    def _run(
        self,
        query: str,
        max_results: Any = 5,
        exclude_ingredients: Optional[List[str]] = None,
        must_include_ingredients: Optional[List[str]] = None,
        dietary_restrictions: Optional[List[str]] = None,
    ) -> str:
        try:
            max_results = int(max_results)
        except (ValueError, TypeError):
            max_results = 5

        if not self._db_path.exists():
            return "Recipe database not found."

        try:
            with sqlite3.connect(self._db_path.as_posix()) as conn:
                q = f"%{query}%"
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT rowid, Title, Ingredients, Instructions
                    FROM recipes
                    WHERE Title LIKE ? OR Ingredients LIKE ?
                    LIMIT ?
                    """,
                    (q, q, max_results * 3),
                )
                rows = cur.fetchall()
        except Exception as e:
            return f"Database error: {e}"

        results = []
        exclude_ingredients = [e.lower() for e in (exclude_ingredients or [])]
        must_include_ingredients = [m.lower() for m in (must_include_ingredients or [])]

        for rowid, title, ingredients, instructions in rows:
            ing_lower = (ingredients or "").lower()
            if exclude_ingredients and any(e in ing_lower for e in exclude_ingredients):
                continue
            if must_include_ingredients and not all(m in ing_lower for m in must_include_ingredients):
                continue
            if not self._matches_diet(ing_lower, dietary_restrictions):
                continue

            short_instr = instructions or ""
            parts = re.split(r"(?<=[.!?])\s+", short_instr.strip())
            short_instr = " ".join(parts[:3])
            ing_preview = ingredients[:150] if ingredients else ""

            results.append(
                {
                    "id": rowid,
                    "title": title,
                    "ingredients_preview": ing_preview,
                    "instructions_preview": short_instr,
                }
            )
            if len(results) >= max_results:
                break

        if not results:
            return "No matching recipes found in the local database."

        lines = []
        for r in results:
            lines.append(
                f"Title: {r['title']}\n"
                f"Key Ingredients: {r['ingredients_preview']}...\n"
                f"Instructions (shortened): {r['instructions_preview']}\n"
                f"Recipe ID: {r['id']}"
            )
            lines.append("---")
        return "\n".join(lines).strip()


def get_diet_tools() -> List[BaseTool]:
    """All diet tools, loaded only when the router selects the diet domain."""
    return [
        BmrTdeeTool(),
        UnitConvertTool(),
        FoodLookupTool(),
        RecipeSearchTool(),
        WebSearchTool(),
    ]
