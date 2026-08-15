-- Отдельная база для pytest: тесты чистят таблицы и не должны задевать dev-данные
CREATE DATABASE lms_test OWNER lms;
