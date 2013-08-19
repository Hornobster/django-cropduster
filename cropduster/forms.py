import re

from django import forms
from django.contrib.admin.widgets import AdminFileWidget
from django.contrib.contenttypes.generic import BaseGenericInlineFormSet
from django.core import validators
from django.core.files.uploadedfile import UploadedFile
from django.db import models
from django.db.models.fields.files import FieldFile
from django.forms.forms import BoundField
from django.forms.models import ModelMultipleChoiceField, ModelFormMetaclass
from django.core.exceptions import ValidationError

from .models import Image, Thumb
from .widgets import cropduster_widget_factory, CropDusterWidget, CropDusterThumbWidget


class CropDusterFormField(forms.Field):

    sizes = None

    def __init__(self, sizes=None, *args, **kwargs):
        self.sizes = sizes or self.sizes
        kwargs['widget'] = CropDusterWidget(field=self, sizes=self.sizes)
        super(CropDusterFormField, self).__init__(*args, **kwargs)

    def to_python(self, value):
        value = super(CropDusterFormField, self).to_python(value)
        if value in validators.EMPTY_VALUES:
            return None

        # value can be an UploadedFile if the form was submitted with the
        # fallback ImageField formfield
        if isinstance(value, UploadedFile):
            return value

        if isinstance(value, basestring) and not value.isdigit():
            return value

        try:
            value = int(str(value))
        except (ValueError, TypeError):
            raise ValidationError(self.error_messages['invalid'])
        return value


def cropduster_formfield_factory(sizes, widget=None, related=None):
    widget = widget or cropduster_widget_factory(sizes, related=related)
    return type('CropDusterFormField', (CropDusterFormField,), {
        '__module__': CropDusterFormField.__module__,
        'sizes': sizes,
        'widget': widget,
        'related': related,
        'parent_model': getattr(related, 'model', None),
        'rel_field': getattr(related, 'field', None),
    })


class CropDusterThumbField(ModelMultipleChoiceField):

    widget = CropDusterThumbWidget

    def clean(self, value):
        """
        Override default validation so that it doesn't throw a ValidationError
        if a given value is not in the original queryset.
        """
        try:
            value = super(CropDusterThumbField, self).clean(value)
        except ValidationError, e:
            if self.error_messages['required'] in e.messages:
                raise
            elif self.error_messages['list'] in e.messages:
                raise
        return value


class BaseCropDusterInlineFormSet(BaseGenericInlineFormSet):

    model = Image
    fields = ('image', 'thumbs',)
    extra_fields = None
    exclude = None
    sizes = None
    exclude = ["content_type", "object_id"]
    max_num = 1
    can_order = False
    can_delete = True
    extra = 1
    label = "Upload"

    prefix_override = None

    def __init__(self, *args, **kwargs):
        self.label = kwargs.pop('label', None) or self.label
        self.sizes = kwargs.pop('sizes', None) or self.sizes
        self.extra = kwargs.pop('extra', None) or self.extra
        self.extra_fields = kwargs.pop('extra_fields', None) or self.extra_fields
        if hasattr(self.extra_fields, 'iter'):
            for field in self.extra_fields:
                self.fields.append(field)

        if self.prefix_override:
            kwargs['prefix'] = self.prefix_override

        super(BaseCropDusterInlineFormSet, self).__init__(*args, **kwargs)

    def initial_form_count(self):
        """
        In the event that the formset fields never rendered, don't raise a
        ValidationError, but return the sensible value (0)
        """
        try:
            return super(BaseCropDusterInlineFormSet, self).initial_form_count()
        except ValidationError:
            return 0

    def total_form_count(self):
        """See the docstring for initial_form_count()"""
        try:
            return super(BaseCropDusterInlineFormSet, self).total_form_count()
        except ValidationError:
            return 0

    @classmethod
    def get_default_prefix(cls):
        if cls.prefix_override:
            return cls.prefix_override
        else:
            return super(BaseCropDusterInlineFormSet, cls).get_default_prefix()

    def _construct_form(self, i, **kwargs):
        try:
            obj = self.queryset[i]
        except IndexError:
            obj = None
        else:
            self.form.instance = obj
            # If cache machine exists, invalidate
            try:
                obj._default_manager.invalidate(obj)
            except AttributeError:
                pass
            try:
                self.instance._default_manager.invalidate(self.instance)
            except AttributeError:
                pass

        qset_ids = [o.pk for o in self.queryset]
        if obj is not None and obj.pk not in qset_ids:
            qset_ids.append(obj.pk)
            self.queryset = self._queryset = obj._default_manager.filter(pk__in=qset_ids)

        self._pre_construct_form(i, **kwargs)
        form = super(BaseCropDusterInlineFormSet, self)._construct_form(i, **kwargs)

        # Load in initial data if we have it from a previously submitted
        # (but apparently invalidated) form
        if self.data is not None and len(self.data):
            relname_re = re.compile(r'^%s\-(.+)$' % re.escape(self.rel_name))
            rel_keys = [(k, relname_re.match(k)) for k in self.data if k.find(self.rel_name) == 0]
            for key, match in rel_keys:
                if not match:
                    continue
                field_name = match.group(1)
                if field_name in form.fields:
                    form.fields[field_name].initial = self.data.get(key)
        return form

    def _pre_construct_form(self, i, **kwargs):
        """
        Limit the queryset of the thumbs for performance reasons (so that it doesn't
        pull in every available thumbnail into the selectbox)
        """
        queryset = Thumb.objects.none()
        try:
            instance = Image.objects.get(pk=self.queryset[0].id)
        except (IndexError, Image.DoesNotExist):
            if self.data:
                queryset = Thumb.objects.filter(
                    pk__in=map(int, self.data.getlist(self.rel_name + '-0-thumbs')))
        else:
            queryset = instance.thumbs.get_query_set()

        thumb_field = self.form.base_fields['thumbs']
        thumb_field.queryset = queryset
        thumb_widget = getattr(thumb_field.widget, 'widget', thumb_field.widget)
        try:
            thumb_widget.choices.queryset = queryset
        except AttributeError:
            pass


class CropDusterBoundField(BoundField):

    def __init__(self, form, field, name):
        super(CropDusterBoundField, self).__init__(form, field, name)
        db_field = getattr(getattr(field, 'related', None), 'field', None)
        db_image_field = getattr(db_field, 'image_field', None)
        value = self.value()
        use_image_field = False
        # If the ImageFieldFile has a filename, but no corresponding
        # cropduster.Image (as it would, for instance, on instances
        # with images originally saved with a vanilla models.ImageField)
        # then we use the standard ImageField formfield.
        if isinstance(value, FieldFile) and value.name and not value.cropduster_image:
            use_image_field = True
        # If this is a form submission from the ImageField formfield (above),
        # then the value can be a django UploadedFile
        elif isinstance(value, UploadedFile):
            use_image_field = True
        # Swap out the CropDusterFormField with a django.forms.ImageField
        if use_image_field and db_image_field:
            self.field = db_image_field.formfield(**{
                'required': field.required,
                'label': field.label,
                'initial': field.initial,
                'widget': AdminFileWidget,
                'help_text': field.help_text,
            })

    def as_widget(self, widget=None, attrs=None, only_initial=False):
        widget = widget or self.field.widget
        attrs = attrs or {}

        if self.auto_id and 'id' not in attrs and 'id' not in widget.attrs:
            attrs['id'] = self.html_initial_id if only_initial else self.auto_id

        name = self.html_initial_name if only_initial else self.html_name

        widget_kwargs = {'attrs': attrs,}
        if isinstance(widget, CropDusterWidget):
            widget_kwargs['bound_field'] = self

        return widget.render(name, self.value(), **widget_kwargs)


def cropduster_formset_factory(sizes=None, **kwargs):
    model = kwargs.get('model', Image)
    ct_field = model._meta.get_field("content_type")
    ct_fk_field = model._meta.get_field("object_id")
    exclude = [ct_field.name, ct_fk_field.name]

    formfield_callback = kwargs.get('formfield_callback')

    def formfield_for_dbfield(db_field, **kwargs):
        if isinstance(db_field, models.ManyToManyField) and db_field.rel.to == Thumb:
            return db_field.formfield(form_class=CropDusterThumbField)
        elif isinstance(db_field, models.ImageField) and db_field.model == Image:
            kwargs['widget'] = forms.TextInput
        kwargs.pop('request', None)
        if formfield_callback is not None:
            return formfield_callback(db_field, **kwargs)
        else:
            return db_field.formfield(**kwargs)

    model = kwargs.get('model', Image)

    def has_changed(self): return True

    form_class_attrs = {
        'has_changed': has_changed,
        "model": model,
        "image": forms.CharField(required=False),
        "formfield_callback": formfield_for_dbfield,
        "Meta": type('Meta', (object,), {
            "fields": BaseCropDusterInlineFormSet.fields,
            "exclude": exclude,
            "model": model,
        }),
        '__module__': BaseCropDusterInlineFormSet.__module__,
    }

    CropDusterForm = ModelFormMetaclass('CropDusterForm', (forms.ModelForm,), form_class_attrs)

    inline_formset_attrs = {
        "formfield_callback": formfield_for_dbfield,
        "ct_field": ct_field,
        "ct_fk_field": ct_fk_field,
        "exclude": exclude,
        "form": CropDusterForm,
        "model": model,
        '__module__': BaseCropDusterInlineFormSet.__module__,
        'prefix_override': kwargs.get('prefix'),
    }
    if sizes is not None:
        inline_formset_attrs['sizes'] = sizes

    return type('CropDusterInlineFormSet',
        (BaseCropDusterInlineFormSet,), inline_formset_attrs)