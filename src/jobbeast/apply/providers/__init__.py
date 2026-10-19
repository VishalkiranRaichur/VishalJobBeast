from .ashby import AshbyProvider
from .greenhouse import GreenhouseProvider
from .lever import LeverProvider
from .smartrecruiters import SmartRecruitersProvider
from .workday import WorkdayProvider
from .generic import GenericProvider

PROVIDERS = {
    cls.name: cls for cls in (GreenhouseProvider, LeverProvider, AshbyProvider, WorkdayProvider, SmartRecruitersProvider, GenericProvider)
}
