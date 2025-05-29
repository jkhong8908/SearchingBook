import pandas as pd

# 엑셀 파일 경로
LIBRARY_LIST_FILE = 'library_list.xlsx'

# 엑셀 파일 읽기
df = pd.read_excel(LIBRARY_LIST_FILE)

# 데이터프레임에서 원하는 열 일부 출력해보기
print(df[['도서관명', '주소', '도서관코드']].head())  # 상위 5개 행 출력
