from rest_framework import serializers
from .models import Author, Book, Member, Loan
from django.contrib.auth.models import User

class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = '__all__'

class BookSerializer(serializers.ModelSerializer):
    author = AuthorSerializer(read_only=True)
    author_id = serializers.PrimaryKeyRelatedField(
        queryset=Author.objects.all(), source='author', write_only=True
    )

    class Meta:
        model = Book
        fields = ['id', 'title', 'author', 'author_id', 'isbn', 'genre', 'available_copies']

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email']

class MemberSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source='user', write_only=True
    )

    class Meta:
        model = Member
        fields = ['id', 'user', 'user_id', 'membership_date']

class LoanSerializer(serializers.ModelSerializer):
    book = BookSerializer(read_only=True)
    member = MemberSerializer(read_only=True)

    class Meta:
        model = Loan
        fields = ['id', 'book', 'member', 'loan_date', 'due_date', 'return_date', 'is_returned']
        read_only_fields = fields


class LoanMemberSerializer(serializers.Serializer):
    member_id = serializers.PrimaryKeyRelatedField(queryset=Member.objects.all())


class LoanExtensionSerializer(serializers.Serializer):
    additional_days = serializers.IntegerField(min_value=1)
