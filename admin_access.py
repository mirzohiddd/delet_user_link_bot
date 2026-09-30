"""
Admin access system: kim qaysi guruh statistikasini ko'ra oladi.

Authorization FAQAT .env konfiguratsiyasi (GROUP_N_ADMIN_IDS) asosida.
Callback data, xabar matni yoki boshqa foydalanuvchi kiritgan ma'lumotga
hech qachon ishonilmaydi — har bir so'rovda user_id qayta tekshiriladi.
"""

from __future__ import annotations

from group_settings import GroupConfig, GroupSettings


def get_allowed_groups(settings: GroupSettings, user_id: int | None) -> list[GroupConfig]:
    """User admin bo'lgan guruhlar ro'yxati (.env tartibida)."""
    if user_id is None:
        return []
    return [g for g in settings.groups if user_id in g.admin_ids]


def can_view_group(settings: GroupSettings, user_id: int | None, group_id: int) -> bool:
    """User aynan shu guruh statistikasini ko'rishga ruxsatga egami."""
    if user_id is None:
        return False
    group = settings.get_group(group_id)
    return group is not None and user_id in group.admin_ids
