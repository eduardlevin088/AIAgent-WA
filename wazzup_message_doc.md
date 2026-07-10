Вебхук о новых сообщениях, изменении и удалении сообщения
{
  "messages": [
    {
      "messageId": "String (uuid4)",
      "channelId": "String (uuid4)",
      "chatType": "String",
      "chatId": "String",
      "avitoProfileId": "String",
      "dateTime": "String",
      "type": "String",
      "isEcho": "Boolean",
      "contact": {
        "name": "String",
        "avatarUri": "String",
        "username": "String",
        "phone": "String"
      },
      "text": "String",
      "contentUri": "String",
      "status": "String",
      "error": {
        "error": "String",
        "description": "String"
      },
      "authorName": "String",
      "authorId": "String",
      "instPost": { ... },
      "interactive": [ ... ],
      "quotedMessage": { ... },
      "sentFromApp": "Boolean",
      "isEdited": "Boolean",
      "isDeleted": "Boolean",
      "oldInfo": {
        "oldText": "String",
        "oldAuthorId": "String",
        "oldAuthorName": "String"
      }
    }
  ]
}
Вебхук пришлет JSON-объект с ключом messages, в значении которого лежит массив объектов со следующими параметрами:

Параметр	Тип	Описание
messageId	String (uuid4)	guid сообщения в Wazzup
channelId	String (uuid4)	ID канала
chatType	String	Тип чата. Доступные значения: whatsapp, whatsgroup, viber, instagram*, telegram, telegroup, vk, avito, max, maxgroup.
chatId	String	ID чата (аккаунт контакта в мессенджере).
avitoProfileId	String	Id профиля Авито. Не то же, что chatId.
dateTime	String	Время отправки сообщения в формате yyyy-mm-ddThh:mm:ss.ms
type	String	Тип сообщения: text, image, audio, video, document, vcard, geo, wapi_template, unsupported, missing_call, unknown.
isEcho	Boolean	Если сообщение входящее — false. Если исходящее — true
contact	object contact	Информация о контакте
text	String	Текст сообщения. Может отсутствовать, если сообщение с контентом
contentUri	String	Ссылка на контент сообщения. Может отсутствовать, если сообщение не содержит контента
status	String	Содержит только значение из ENUM из вебхука statuses: sent, delivered, read, error, inbound.
error	object error	Приходит, если status: error
authorName	String	Имя пользователя, отправившего сообщение. Может быть только при isEcho == true
authorId	String	Идентификатор пользователя CRM
instPost	object instPost	Информация о посте из Instagram*. Прикладывается к комментарию в Instagram*
interactive	Interactive	Массив объектов с кнопками Salesbot amoCRM
quotedMessage	Object	Объект с параметрами цитируемого сообщения
sentFromApp	Boolean	true, если отправлено из нативного чата Wazzup
isEdited	Boolean	Показывает, что сообщение отредактировано.
isDeleted	Boolean	Показывает, что сообщение удалено.
oldInfo	object oldInfo	Содержит информацию об измененном или удаленном сообщении
contact (объект)
Параметр	Тип	Описание
name	String	Имя контакта
avatarUri	String	URI аватарки контакта.
username	String	Только для Telegram. username (имя пользователя) без @
phone	String	Только для Telegram, MAX. Телефон в международном формате
error (объект)
Параметр	Тип	Описание
error	String	Код ошибки (BAD_CONTACT, CHATID_IGSID_MISMATCH, TOO_LONG_TEXT, SPAM и др.)
description	String	Описание ошибки
instPost (объект)
Параметр	Тип	Описание
id	String	ID поста
src	String	Ссылка на пост
author	String	Автор поста
description	String	Описание поста
oldInfo (объект)
Параметр	Тип	Описание
oldText	String	Текст сообщения до редактирования или удаления
oldAuthorId	String	id автора исходного сообщения
oldAuthorName	String	Имя автора исходного сообщения
