from typing import List, Optional
from pydantic import BaseModel, HttpUrl

class Location(BaseModel):
    city: Optional[str] = None
    country: Optional[str] = None
    raw_location: Optional[str] = None

class ExperienceItem(BaseModel):
    title: Optional[str] = None
    company_name: Optional[str] = None
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = "Present"
    description: Optional[str] = None
    employment_type: Optional[str] = None

class EducationItem(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None

class ProfileRequest(BaseModel):
    profile_url: HttpUrl
    # Sections (experience/education/skills/...) cost ~8 extra LinkedIn calls per
    # profile. On by default because they are the point of the service; set
    # false for the cheap intro-card-only path.
    include_sections: bool = True

class ResponseMeta(BaseModel):
    """Provenance for the caller: when it was fetched, how long it took, whether
    it came from the cache, and which SDUI section cards were actually read."""
    fetched_at: Optional[str] = None
    elapsed_ms: Optional[int] = None
    cached: bool = False
    sections_requested: bool = True
    section_cards_fetched: List[str] = []


class ProfileResponse(BaseModel):
    profile_url: str
    profile_handle: str
    full_name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[Location] = None
    about: Optional[str] = None
    profile_image_url: Optional[str] = None
    experience: List[ExperienceItem] = []
    education: List[EducationItem] = []
    skills: List[str] = []
    certifications: List[str] = []
    languages: List[str] = []
    volunteering: List[ExperienceItem] = []  # title=role, company_name=organisation
    meta: Optional[ResponseMeta] = None