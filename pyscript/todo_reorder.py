@service
def todo_reorder(entity_id="todo.shopping_list", uids=None):
    """Reorder to-do items to match the given uid order. Moves only - nothing is re-added."""
    entity = hass.data["todo"].get_entity(entity_id)
    if entity is None:
        log.error(f"todo_reorder: {entity_id} not found")
        return
    prev = None
    for uid in uids or []:
        entity.async_move_todo_item(uid=uid, previous_uid=prev)
        prev = uid
